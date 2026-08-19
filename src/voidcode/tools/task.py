from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..runtime.contracts import (
    BackgroundTaskResult,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionResult,
    runtime_subagent_route_from_metadata,
    validate_runtime_request_metadata,
)
from ..runtime.task import BackgroundTaskState, StoredBackgroundTaskSummary
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import require_runtime_tool_context


class TaskRuntime(Protocol):
    def run(self, request: RuntimeRequest) -> RuntimeResponse: ...

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState: ...

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def session_result(self, *, session_id: str) -> RuntimeSessionResult: ...


class _TaskArgs(BaseModel):
    prompt: str
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

    @field_validator("prompt", mode="after")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must be a non-empty string")
        return stripped

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


class TaskTool:
    definition = ToolDefinition(
        name="task",
        description=(
            "Delegate work to a child runtime session. Always include prompt, "
            "run_in_background, load_skills, and subagent_type. Prefer run_in_background=true "
            "for delegated work that can run independently. Each background task emits a "
            "completion event to its parent; when launching several tasks for one deliverable, "
            "wait for all known tasks to finish before synthesizing the final answer."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Full delegated task prompt for the child session.",
                    "minLength": 1,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Required. true starts delegated work in the background and returns "
                        "a task_id. false runs the child session synchronously. Prefer true "
                        "for independent delegated work."
                    ),
                },
                "load_skills": {
                    "type": "array",
                    "description": ("Required. Array of skill names to force-load in the child session. Pass [] when no extra skills are needed."),
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                },
                "subagent_type": {
                    "type": "string",
                    "description": ("Required. Explicit child preset: advisor, explore, researcher, worker, or product."),
                    "minLength": 1,
                },
                "description": {
                    "type": "string",
                    "description": "Optional short delegation description.",
                    "minLength": 1,
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional existing child session id to continue.",
                    "minLength": 1,
                },
                "command": {
                    "type": "string",
                    "description": "Optional originating command label for delegated work.",
                    "minLength": 1,
                },
                "parallel_group_id": {
                    "type": "string",
                    "description": "Optional shared id for parallel tasks serving one deliverable.",
                    "minLength": 1,
                },
                "parallel_group_size": {
                    "type": "integer",
                    "description": "Expected number of tasks in the parallel group.",
                    "minimum": 1,
                },
                "keep_alive": {
                    "type": "boolean",
                    "description": (
                        "Optional. true keeps the delegated child session alive across steer "
                        "turns: after each turn without a handoff the task parks as idle "
                        "(awaiting_steer) and the leader resumes it with steer_task. Requires "
                        "run_in_background=true."
                    ),
                },
                "outputSchema": {
                    "type": "object",
                    "description": (
                        "Optional arbitrary JSON Schema (outputSchema) declaring the structured "
                        "shape of the child's submit_result data. The child's final data is "
                        "validated against this schema at task finalize and surfaced as "
                        "structured_output. Requires run_in_background=true."
                    ),
                },
                "schemaMode": {
                    "type": "string",
                    "enum": ["permissive", "strict"],
                    "description": (
                        "Optional validation strictness for outputSchema. permissive (default) "
                        "keeps the task completed with schema_validation.valid=false on "
                        "failure; strict fails the task with the validation error. Ignored "
                        "without outputSchema."
                    ),
                },
            },
            "required": ["prompt", "run_in_background", "load_skills", "subagent_type"],
            "examples": [
                {
                    "prompt": "Find where background task cancellation is implemented.",
                    "run_in_background": True,
                    "load_skills": [],
                    "subagent_type": "explore",
                },
                {
                    "prompt": "Review the architecture tradeoffs and summarize them.",
                    "run_in_background": False,
                    "load_skills": [],
                    "subagent_type": "advisor",
                },
            ],
        },
        read_only=True,
    )

    def __init__(self, *, runtime: TaskRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _TaskArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        context = require_runtime_tool_context(self.definition.name)
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
                delegation_metadata["remaining_spawn_budget"] = max(
                    context.remaining_spawn_budget - 1,
                    0,
                )
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
            task = self._runtime.start_background_task(request)
            retry_guidance = (
                "Continue other safe work now. Do not call background_output immediately "
                "unless you need a real status check; prefer waiting for a completion "
                "reminder, or use background_output(block=true) when you intentionally "
                "want to wait in the current turn."
            )
            waiting_reason = task.observability.waiting_reason if task.observability is not None else None
            keep_alive_guidance = (
                " This task is keep-alive: after each turn without a final result the worker "
                "parks as idle and emits runtime.background_task_awaiting_steer; dispatch the "
                "next instruction with steer_task(task_id=..., prompt=...) and repeat until "
                "the worker submits its final result."
                if args.keep_alive
                else ""
            )
            if task.status == "queued":
                queued_reason = waiting_reason or "queued"
                content = (
                    f"Started background task {task.task.id} (status: queued; reason: {queued_reason}). "
                    "It will be dispatched when capacity is available; continue other work now and "
                    "do not call background_output immediately unless you truly need a status check. "
                    "Wait for a completion reminder, or use background_output(block=true) when you "
                    "intentionally need to wait."
                    f"{keep_alive_guidance}"
                )
            else:
                content = (
                    f"Started background task {task.task.id}. Continue other work now; "
                    "do not call background_output immediately unless you truly need a "
                    "status check. Wait for a completion reminder, or use "
                    "background_output(block=true) when you intentionally need to wait."
                    f"{keep_alive_guidance}"
                )
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=content,
                data={
                    "task_id": task.task.id,
                    "status": task.status,
                    "parent_session_id": context.session_id,
                    "child_session_id": task.session_id,
                    "delegation": dict(delegation_payload),
                    "result_available": task.result_available,
                    "requested_subagent_type": args.subagent_type,
                    "load_skills": list(args.load_skills),
                    "waiting_reason": waiting_reason,
                    "keep_alive": args.keep_alive,
                },
                retry_guidance=retry_guidance,
            )

        response = self._runtime.run(request)
        session = response.session
        output = getattr(response, "output", None)
        status = session.status
        child_session = session.session
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output if isinstance(output, str) and output else f"Delegated session {child_session.id}",
            data={
                "session_id": child_session.id,
                "parent_session_id": context.session_id,
                "status": status,
                "requested_subagent_type": args.subagent_type,
                "load_skills": list(args.load_skills),
                **({"output": output} if output is not None else {}),
            },
        )
