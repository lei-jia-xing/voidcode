from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator, model_validator

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
    category: str | None = None
    subagent_type: str | None = None
    description: str | None = None
    session_id: str | None = None
    command: str | None = None

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
        "category", "subagent_type", "description", "session_id", "command", mode="after"
    )
    @classmethod
    def _strip_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _validate_route(self) -> _TaskArgs:
        if bool(self.category) == bool(self.subagent_type):
            raise ValueError("provide exactly one of category or subagent_type")
        return self


def _delegation_metadata(args: _TaskArgs) -> dict[str, str]:
    metadata: dict[str, str] = {
        "mode": "background" if args.run_in_background else "sync",
    }
    if args.category is not None:
        metadata["category"] = args.category
    if args.subagent_type is not None:
        metadata["subagent_type"] = args.subagent_type
    if args.description is not None:
        metadata["description"] = args.description
    if args.command is not None:
        metadata["command"] = args.command
    return metadata


def _delegated_prompt(args: _TaskArgs) -> str:
    routing = [
        "Delegated runtime task.",
        f"Requested mode: {'background' if args.run_in_background else 'sync'}",
        f"Requested category: {args.category}" if args.category else None,
        f"Requested subagent_type: {args.subagent_type}" if args.subagent_type else None,
        f"Short description: {args.description}" if args.description else None,
        f"Requested command: {args.command}" if args.command else None,
        (
            "Requested load_skills: " + ", ".join(args.load_skills)
            if args.load_skills
            else "Requested load_skills: (none)"
        ),
        "",
        "Task:",
        args.prompt,
    ]
    return "\n".join(line for line in routing if line is not None).strip()


class TaskTool:
    definition = ToolDefinition(
        name="task",
        description=(
            "Delegate work to a child runtime session. Always include prompt, "
            "run_in_background, and load_skills. Provide exactly one of category or "
            "subagent_type. Prefer run_in_background=true for delegated work that can "
            "run independently."
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
                    "description": (
                        "Required. Array of skill names to force-load in the child session. "
                        "Pass [] when no extra skills are needed."
                    ),
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Runtime-selected child route. Provide this or subagent_type, but not both."
                    ),
                    "minLength": 1,
                },
                "subagent_type": {
                    "type": "string",
                    "description": (
                        "Explicit child preset. Provide this or category, but not both."
                    ),
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
            },
            "required": ["prompt", "run_in_background", "load_skills"],
            "oneOf": [
                {"required": ["category"], "not": {"required": ["subagent_type"]}},
                {"required": ["subagent_type"], "not": {"required": ["category"]}},
            ],
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
                    "category": "writing",
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
            prompt=_delegated_prompt(args),
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
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=(
                    f"Started background task {task.task.id}. Continue other work now; "
                    "do not call background_output immediately unless you truly need a "
                    "status check. Wait for a completion reminder, or use "
                    "background_output(block=true) when you intentionally need to wait."
                ),
                data={
                    "task_id": task.task.id,
                    "status": task.status,
                    "parent_session_id": context.session_id,
                    "child_session_id": task.session_id,
                    "delegation": dict(delegation_payload),
                    "result_available": task.result_available,
                    "requested_category": args.category,
                    "requested_subagent_type": args.subagent_type,
                    "load_skills": list(args.load_skills),
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
            content=output
            if isinstance(output, str) and output
            else f"Delegated session {child_session.id}",
            data={
                "session_id": child_session.id,
                "parent_session_id": context.session_id,
                "status": status,
                "requested_category": args.category,
                "requested_subagent_type": args.subagent_type,
                "load_skills": list(args.load_skills),
                **({"output": output} if output is not None else {}),
            },
        )
