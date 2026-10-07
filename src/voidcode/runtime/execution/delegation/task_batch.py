from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import NonEmptyPrompt, parse_tool_args
from ....tools.contracts import TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess
from ....tools.delegation.task_batch import MAX_BATCH_SIZE, TaskBatchTool
from ...background.models import BackgroundTaskState
from ...contracts import (
    RuntimeRequest,
    RuntimeRequestError,
    runtime_subagent_route_from_metadata,
    validate_runtime_request_metadata,
)


@dataclass(frozen=True, slots=True)
class TaskBatchCreated:
    index: int
    task_id: str
    status: str
    requested_subagent_type: str
    load_skills: tuple[str, ...]
    child_session_id: str | None
    result_available: bool
    waiting_reason: str | None

    def as_payload(self) -> dict[str, object]:
        return {
            "index": self.index,
            "task_id": self.task_id,
            "status": self.status,
            "requested_subagent_type": self.requested_subagent_type,
            "load_skills": list(self.load_skills),
            "child_session_id": self.child_session_id,
            "result_available": self.result_available,
            "waiting_reason": self.waiting_reason,
        }


@dataclass(frozen=True, slots=True)
class TaskBatchFailed:
    index: int
    requested_subagent_type: str
    load_skills: tuple[str, ...]
    error: str

    def as_payload(self) -> dict[str, object]:
        return {
            "index": self.index,
            "status": "failed",
            "requested_subagent_type": self.requested_subagent_type,
            "load_skills": list(self.load_skills),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TaskBatchResultBody:
    parallel_group_id: str
    parallel_group_size: int
    task_ids: tuple[str, ...]
    created: tuple[TaskBatchCreated, ...]
    failed: tuple[TaskBatchFailed, ...]
    retrieval_instruction: str

    def as_payload(self) -> dict[str, object]:
        return {
            "parallel_group_id": self.parallel_group_id,
            "parallel_group_size": self.parallel_group_size,
            "task_ids": list(self.task_ids),
            "created_count": len(self.created),
            "failed_count": len(self.failed),
            "partial": bool(self.failed),
            "created": [item.as_payload() for item in self.created],
            "failed": [item.as_payload() for item in self.failed],
            "retrieval_instruction": self.retrieval_instruction,
        }


class TaskBatchRuntime(Protocol):
    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState: ...


class _BatchItemArgs(BaseModel):
    """One fixed-background child request accepted by ``task_batch``."""

    model_config = ConfigDict(extra="forbid")

    prompt: NonEmptyPrompt
    load_skills: list[str]
    subagent_type: str
    description: str | None = None
    command: str | None = None
    output_schema: dict[str, object] | None = Field(default=None, validation_alias="outputSchema")
    schema_mode: Literal["permissive", "strict"] = Field(default="permissive", validation_alias="schemaMode")

    @field_validator("load_skills", mode="after")
    @classmethod
    def _validate_load_skills(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"load_skills[{index}] must be a non-empty string")
            normalized.append(item.strip())
        return normalized

    @field_validator("subagent_type", "description", "command", mode="after")
    @classmethod
    def _strip_strings(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            if info.field_name == "subagent_type":
                raise ValueError("subagent_type must be a non-empty string")
            return None
        return stripped

    @model_validator(mode="after")
    def _validate_output_schema(self) -> _BatchItemArgs:
        if self.schema_mode == "strict" and self.output_schema is None:
            raise ValueError("schemaMode=strict requires outputSchema")
        return self


class _TaskBatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[_BatchItemArgs]

    @field_validator("tasks", mode="after")
    @classmethod
    def _validate_tasks(cls, value: list[_BatchItemArgs]) -> list[_BatchItemArgs]:
        if not value:
            raise ValueError("tasks must contain at least one child request")
        if len(value) > MAX_BATCH_SIZE:
            raise ValueError(f"tasks may contain at most {MAX_BATCH_SIZE} child requests")
        fingerprints = {
            json.dumps(
                item.model_dump(by_alias=True, exclude_none=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in value
        }
        if len(fingerprints) != len(value):
            raise ValueError("tasks must not contain duplicates")
        return value


class TaskBatchCommand:
    def __init__(self, *, runtime: TaskBatchRuntime) -> None:
        self._runtime = runtime

    @staticmethod
    def _delegation_metadata(
        item: _BatchItemArgs,
        *,
        group_id: str,
        group_size: int,
        depth: int,
        remaining_spawn_budget: int | None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "mode": "background",
            "subagent_type": item.subagent_type,
            "parallel_group_id": group_id,
            "parallel_group_size": group_size,
        }
        if item.description is not None:
            metadata["description"] = item.description
        if item.command is not None:
            metadata["command"] = item.command
        if item.output_schema is not None:
            metadata["output_schema"] = item.output_schema
            metadata["schema_mode"] = item.schema_mode
        if depth > 0 or remaining_spawn_budget is not None:
            metadata["depth"] = depth + 1
            if remaining_spawn_budget is not None:
                metadata["remaining_spawn_budget"] = remaining_spawn_budget - group_size
        return metadata

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if call.tool_name != TaskBatchTool.definition.name:
            raise ValueError("task batch command requires its selected task_batch binding")
        context.require_session_id()
        args = parse_tool_args(_TaskBatchArgs, call.arguments, tool_name=TaskBatchTool.definition.name)
        if context.remaining_spawn_budget is not None and context.remaining_spawn_budget < len(args.tasks):
            raise ValueError(
                f"task_batch requires {len(args.tasks)} child spawn slots, but parent session has {context.remaining_spawn_budget} remaining"
            )

        # Build and validate every request before the first runtime call. This is
        # deliberately a preflight phase: invalid presets, schemas, or metadata
        # cannot leave an orphaned prefix of the batch behind.
        group_id = f"batch-{uuid4().hex}"
        group_size = len(args.tasks)
        requests: list[tuple[int, _BatchItemArgs, RuntimeRequest]] = []
        for index, item in enumerate(args.tasks):
            delegation = self._delegation_metadata(
                item,
                group_id=group_id,
                group_size=group_size,
                depth=context.delegation_depth,
                remaining_spawn_budget=context.remaining_spawn_budget,
            )
            request_metadata: dict[str, object] = {
                "force_load_skills": list(item.load_skills),
                "delegation": delegation,
            }
            try:
                validated_metadata = validate_runtime_request_metadata(request_metadata)
                _ = runtime_subagent_route_from_metadata(validated_metadata)
            except (RuntimeRequestError, ValueError) as exc:
                raise ValueError(f"task_batch item {index} is invalid: {exc}") from exc
            requests.append(
                (
                    index,
                    item,
                    RuntimeRequest(
                        prompt=item.prompt,
                        parent_session_id=context.session_id,
                        metadata=validated_metadata,
                        allocate_session_id=True,
                    ),
                )
            )

        created: list[TaskBatchCreated] = []
        failed: list[TaskBatchFailed] = []
        task_ids: list[str] = []
        for index, item, request in requests:
            try:
                task = self._runtime.start_background_task(request)
            except Exception as exc:
                failed.append(
                    TaskBatchFailed(
                        index=index,
                        requested_subagent_type=item.subagent_type,
                        load_skills=tuple(item.load_skills),
                        error=str(exc),
                    )
                )
                continue
            task_ids.append(task.task.id)
            waiting_reason = task.observability.waiting_reason if task.observability is not None else None
            created.append(
                TaskBatchCreated(
                    index=index,
                    task_id=task.task.id,
                    status=task.status,
                    requested_subagent_type=item.subagent_type,
                    load_skills=tuple(item.load_skills),
                    child_session_id=task.session_id,
                    result_available=task.result_available,
                    waiting_reason=waiting_reason,
                )
            )

        partial = bool(failed)
        retrieval_instruction = (
            f'task(operation="output", parallel_group_id="{group_id}")'
            if not partial
            else "Use the returned task_ids only after the partial batch is reconciled; no automatic retry or cancellation was performed."
        )
        body = TaskBatchResultBody(
            parallel_group_id=group_id,
            parallel_group_size=group_size,
            task_ids=tuple(task_ids),
            created=tuple(created),
            failed=tuple(failed),
            retrieval_instruction=retrieval_instruction,
        )
        if not created:
            error = "task_batch could not dispatch any child request"
            return ToolFailure(
                tool_name=TaskBatchTool.definition.name,
                error=error,
                output=TextOutput(error),
                body=body,
            )
        if partial:
            content = (
                f"Partially dispatched task batch {group_id}: created {len(created)}/{group_size}; "
                f"failed item indexes {[item.index for item in failed]}. Created tasks remain active; "
                "no automatic retry or cancellation was performed."
            )
        else:
            content = (
                f"Started task batch {group_id} with {group_size} background tasks. "
                f'Read the group with task(operation="output", parallel_group_id="{group_id}").'
            )
        return ToolSuccess(
            tool_name=TaskBatchTool.definition.name,
            output=TextOutput(content),
            body=body,
        )
