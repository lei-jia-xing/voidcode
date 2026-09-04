from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ...runtime.background.models import BackgroundTaskState
from ...runtime.contracts import (
    RuntimeRequest,
    RuntimeRequestError,
    runtime_subagent_route_from_metadata,
    validate_runtime_request_metadata,
)
from .._pydantic_args import format_validation_error
from ..contracts import ToolCall, ToolDefinition, ToolResult
from ..runtime_context import require_runtime_tool_context

_MAX_BATCH_SIZE = 100


class TaskBatchRuntime(Protocol):
    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState: ...


class _BatchItemArgs(BaseModel):
    """One fixed-background child request accepted by ``task_batch``."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    load_skills: list[str]
    subagent_type: str
    description: str | None = None
    command: str | None = None
    output_schema: dict[str, object] | None = Field(default=None, validation_alias="outputSchema")
    schema_mode: Literal["permissive", "strict"] = Field(default="permissive", validation_alias="schemaMode")

    @field_validator("prompt", mode="after")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must be a non-empty string")
        return stripped

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
    def _strip_strings(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            if getattr(info, "field_name", None) == "subagent_type":
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
        if len(value) > _MAX_BATCH_SIZE:
            raise ValueError(f"tasks may contain at most {_MAX_BATCH_SIZE} child requests")
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


class TaskBatchTool:
    definition = ToolDefinition(
        name="task_batch",
        description=(
            "Dispatch a bounded batch of independent child sessions in the background under one "
            "runtime-owned parallel group. Each item must declare prompt, load_skills, and "
            "subagent_type. The runtime supplies parent ownership and group metadata; nested "
            "graphs, dependencies, retries, cancellation, and keep-alive are not supported."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": _MAX_BATCH_SIZE,
                    "description": ("Independent child requests. All children run in the background and share one runtime-owned parallel group."),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Full delegated task prompt for this child session.",
                            },
                            "load_skills": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "description": "Skill names to force-load in this child; pass [] when none are needed.",
                            },
                            "subagent_type": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Explicit child preset: advisor, explore, researcher, worker, or product.",
                            },
                            "description": {"type": "string", "minLength": 1},
                            "command": {"type": "string", "minLength": 1},
                            "outputSchema": {
                                "type": "object",
                                "description": "Optional JSON Schema for this child's yield data.",
                            },
                            "schemaMode": {
                                "type": "string",
                                "enum": ["permissive", "strict"],
                                "description": "Validation mode for outputSchema; strict requires outputSchema.",
                            },
                        },
                        "required": ["prompt", "load_skills", "subagent_type"],
                    },
                }
            },
            "required": ["tasks"],
            "examples": [
                {
                    "tasks": [
                        {
                            "prompt": "Inspect the auth flow",
                            "load_skills": [],
                            "subagent_type": "explore",
                        },
                        {
                            "prompt": "Review the storage boundary",
                            "load_skills": [],
                            "subagent_type": "researcher",
                        },
                    ]
                }
            ],
        },
        read_only=True,
    )

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

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _TaskBatchArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        context = require_runtime_tool_context(self.definition.name)
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

        created: list[dict[str, object]] = []
        failed: list[dict[str, object]] = []
        task_ids: list[str] = []
        for index, item, request in requests:
            try:
                task = self._runtime.start_background_task(request)
            except Exception as exc:
                failed.append(
                    {
                        "index": index,
                        "status": "failed",
                        "requested_subagent_type": item.subagent_type,
                        "load_skills": list(item.load_skills),
                        "error": str(exc),
                    }
                )
                continue
            task_ids.append(task.task.id)
            waiting_reason = task.observability.waiting_reason if task.observability is not None else None
            created.append(
                {
                    "index": index,
                    "task_id": task.task.id,
                    "status": task.status,
                    "requested_subagent_type": item.subagent_type,
                    "load_skills": list(item.load_skills),
                    "child_session_id": task.session_id,
                    "result_available": task.result_available,
                    "waiting_reason": waiting_reason,
                }
            )

        partial = bool(failed)
        payload: dict[str, object] = {
            "parallel_group_id": group_id,
            "parallel_group_size": group_size,
            "task_ids": task_ids,
            "created_count": len(created),
            "failed_count": len(failed),
            "partial": partial,
            "created": created,
            "failed": failed,
            "retrieval_instruction": (
                f'background_task(operation="output", parallel_group_id="{group_id}")'
                if not partial
                else "Use the returned task_ids only after the partial batch is reconciled; no automatic retry or cancellation was performed."
            ),
        }
        if not created:
            error = "task_batch could not dispatch any child request"
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                content=error,
                data=payload,
                error=error,
            )
        if partial:
            content = (
                f"Partially dispatched task batch {group_id}: created {len(created)}/{group_size}; "
                f"failed item indexes {[item['index'] for item in failed]}. Created tasks remain active; "
                "no automatic retry or cancellation was performed."
            )
        else:
            content = (
                f"Started task batch {group_id} with {group_size} background tasks. "
                f'Read the group with background_task(operation="output", parallel_group_id="{group_id}").'
            )
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=payload,
        )


__all__ = ["TaskBatchTool"]
