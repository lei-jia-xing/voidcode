from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from ....core.tool_context import ToolContext
from ....security.json_values import json_wire_object, own_json_object
from ....tools._pydantic_args import NonEmptyPrompt, parse_tool_args
from ....tools.contracts import EmptyOutput, TextOutput, ToolCall, ToolResult, ToolSuccess
from ....tools.delegation.task import TaskTool
from ...background.models import BackgroundTaskState
from ...background.substrate import TaskSpec, TaskSubstrate
from ...contracts import (
    RuntimeRequest,
    RuntimeResponse,
    runtime_subagent_route_from_metadata,
    validate_runtime_request_metadata,
)
from .task_control import TaskControlRuntime, TaskControlTool


@dataclass(frozen=True, slots=True)
class BackgroundTaskStartedBody:
    task_id: str
    status: str
    parent_session_id: str | None
    child_session_id: str | None
    delegation: Mapping[str, object]
    result_available: bool
    requested_subagent_type: str
    load_skills: tuple[str, ...]
    waiting_reason: str | None
    keep_alive: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "delegation", own_json_object(self.delegation))

    def as_payload(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "delegation": json_wire_object(self.delegation),
            "result_available": self.result_available,
            "requested_subagent_type": self.requested_subagent_type,
            "load_skills": list(self.load_skills),
            "waiting_reason": self.waiting_reason,
            "keep_alive": self.keep_alive,
        }


@dataclass(frozen=True, slots=True)
class TaskCompletedBody:
    session_id: str
    parent_session_id: str | None
    status: str
    requested_subagent_type: str
    load_skills: tuple[str, ...]
    output: str | None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "status": self.status,
            "requested_subagent_type": self.requested_subagent_type,
            "load_skills": list(self.load_skills),
        }
        if self.output is not None:
            payload["output"] = self.output
        return payload


class TaskRuntime(TaskControlRuntime, Protocol):
    def run(self, request: RuntimeRequest) -> RuntimeResponse: ...

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState: ...


class _TaskArgs(BaseModel):
    prompt: NonEmptyPrompt
    run_in_background: bool
    load_skills: list[str]
    subagent_type: str
    description: str | None = None
    session_id: str | None = None
    command: str | None = None
    parallel_group_id: str | None = None
    parallel_group_size: int | None = None
    keep_alive: bool = False
    output_schema: dict[str, object] | None = Field(default=None, validation_alias="outputSchema")
    schema_mode: Literal["permissive", "strict"] = Field(default="permissive", validation_alias="schemaMode")

    @model_validator(mode="after")
    def _validate_keep_alive(self) -> _TaskArgs:
        if self.keep_alive and not self.run_in_background:
            raise ValueError("keep_alive=true requires run_in_background=true (sync delegation has no suspend/resume semantics)")
        return self

    @model_validator(mode="after")
    def _validate_output_schema(self) -> _TaskArgs:
        if self.output_schema is not None or self.schema_mode != "permissive":
            if not self.run_in_background:
                raise ValueError("outputSchema requires run_in_background=true (sync delegation has no persisted schema validation)")
        if self.schema_mode == "strict" and self.output_schema is None:
            raise ValueError("schemaMode=strict requires outputSchema (schema_mode is meaningless without a declared schema)")
        return self

    @field_validator("load_skills", mode="before")
    @classmethod
    def _parse_load_skills(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return value
        return parsed

    @field_validator("load_skills", mode="after")
    @classmethod
    def _validate_load_skills(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for index, item in enumerate(value):
            if not item.strip():
                raise ValueError(f"load_skills[{index}] must be a non-empty string")
            normalized.append(item.strip())
        return normalized

    @field_validator(
        "subagent_type",
        "description",
        "session_id",
        "command",
        "parallel_group_id",
        mode="after",
    )
    @classmethod
    def _strip_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("parallel_group_size", mode="after")
    @classmethod
    def _validate_parallel_group_size(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("parallel_group_size must be at least 1")
        return value


def _delegation_metadata(args: _TaskArgs) -> dict[str, object]:
    metadata: dict[str, object] = {
        "mode": "background" if args.run_in_background else "sync",
    }
    metadata["subagent_type"] = args.subagent_type
    if args.description is not None:
        metadata["description"] = args.description
    if args.command is not None:
        metadata["command"] = args.command
    if args.parallel_group_id is not None:
        metadata["parallel_group_id"] = args.parallel_group_id
    if args.parallel_group_size is not None:
        metadata["parallel_group_size"] = str(args.parallel_group_size)
    if args.output_schema is not None:
        metadata["output_schema"] = args.output_schema
        metadata["schema_mode"] = args.schema_mode
    return metadata


class TaskCommand:
    def __init__(self, *, runtime: TaskRuntime, substrate: TaskSubstrate | None = None) -> None:
        self._runtime = runtime
        self._substrate = substrate or getattr(runtime, "task_substrate", None)
        self._control = TaskControlTool(runtime=runtime, substrate=self._substrate)

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if call.tool_name != TaskTool.definition.name:
            raise ValueError("task command requires its selected task binding")
        context.require_session_id()
        if "operation" in call.arguments:
            return self._control.invoke(call, context=context)
        args = parse_tool_args(_TaskArgs, call.arguments, tool_name=TaskTool.definition.name)
        delegation_metadata: dict[str, object] = dict(_delegation_metadata(args).items())
        request_metadata: dict[str, object] = {
            "force_load_skills": list(args.load_skills),
            "delegation": delegation_metadata,
        }
        if args.keep_alive:
            request_metadata["keep_alive"] = True
        if context.delegation_depth > 0 or context.remaining_spawn_budget is not None:
            delegation_metadata["depth"] = context.delegation_depth + 1
            if context.remaining_spawn_budget is not None:
                delegation_metadata["remaining_spawn_budget"] = max(context.remaining_spawn_budget - 1, 0)
        validated_metadata = validate_runtime_request_metadata(request_metadata)
        _ = runtime_subagent_route_from_metadata(validated_metadata)
        delegation_payload = validated_metadata.get("delegation")
        assert isinstance(delegation_payload, dict)
        request = RuntimeRequest(
            prompt=args.prompt,
            session_id=args.session_id,
            parent_session_id=context.session_id,
            metadata=validated_metadata,
            allocate_session_id=args.session_id is None,
        )

        if args.run_in_background:
            if self._substrate is not None:
                spec = TaskSpec.from_runtime_request(request, keep_alive=args.keep_alive)
                task = self._substrate.start_task(spec)
            else:
                task = self._runtime.start_background_task(request)
            waiting_reason = task.observability.waiting_reason if task.observability is not None else None
            keep_alive_guidance = (
                " This task is keep-alive: after each turn without a terminal yield the worker parks as idle "
                "and emits runtime.background_task_awaiting_steer; dispatch the next instruction with "
                "task(operation=steer, task_id=..., prompt=...) until the worker submits its final result."
                if args.keep_alive
                else ""
            )
            if task.status == "queued":
                queued_reason = waiting_reason or "queued"
                content = (
                    f"Started background task {task.task.id} (status: queued; reason: {queued_reason}). "
                    "Continue other work; use task(operation=output) only when a status check is needed. "
                    "Wait for a completion reminder or use task(operation=output, block=true) intentionally."
                    f"{keep_alive_guidance}"
                )
            else:
                content = (
                    f"Started background task {task.task.id}. Continue other work; use "
                    "task(operation=output) only when a status check is needed. Wait for a completion "
                    "reminder or use task(operation=output, block=true) intentionally."
                    f"{keep_alive_guidance}"
                )
            return ToolSuccess(
                tool_name=TaskTool.definition.name,
                output=TextOutput(content),
                body=BackgroundTaskStartedBody(
                    task_id=task.task.id,
                    status=task.status,
                    parent_session_id=context.session_id,
                    child_session_id=task.session_id,
                    delegation=delegation_payload,
                    result_available=task.result_available,
                    requested_subagent_type=args.subagent_type,
                    load_skills=tuple(args.load_skills),
                    waiting_reason=waiting_reason,
                    keep_alive=args.keep_alive,
                ),
            )

        response = self._runtime.run(request)
        session = response.session
        output = response.output
        status = session.status
        child_session = session.session
        return ToolSuccess(
            tool_name=TaskTool.definition.name,
            output=TextOutput(output) if output is not None else EmptyOutput(),
            body=TaskCompletedBody(
                session_id=child_session.id,
                parent_session_id=context.session_id,
                status=status,
                requested_subagent_type=args.subagent_type,
                load_skills=tuple(args.load_skills),
                output=output,
            ),
        )
