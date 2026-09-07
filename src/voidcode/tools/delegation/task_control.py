from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .._pydantic_args import format_validation_error
from ..contracts import ToolCall, ToolDefinition, ToolResult
from .task_cancel import TaskCancelRuntime, TaskCancelTool
from .task_output import TaskOutputRuntime, TaskOutputTool
from .task_ps import TaskPsRuntime, TaskPsTool
from .task_steer import TaskSteerRuntime, TaskSteerTool


def _rewrite_background_reference(value: object) -> object:
    """Rewrite internal delegate guidance before it crosses the model boundary."""
    if isinstance(value, str):
        replacements = (
            ("task_output(task_id=", 'task(operation="output", task_id='),
            ("task_output(task_ids=", 'task(operation="output", task_ids='),
            ("task_output(parallel_group_id=", 'task(operation="output", parallel_group_id='),
            ("task_output(block=true)", 'task(operation="output", block=true)'),
            ("task_output", 'task(operation="output")'),
            ("task_cancel", 'task(operation="cancel")'),
            ("task_ps", 'task(operation="ps")'),
            ("task_steer", 'task(operation="steer")'),
        )
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_rewrite_background_reference(item) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_background_reference(item) for key, item in value.items()}
    return value


class TaskControlRuntime(TaskOutputRuntime, TaskCancelRuntime, TaskPsRuntime, TaskSteerRuntime, Protocol):
    """Runtime authority used by the unified task control facade."""


class _TaskControlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["output", "cancel", "ps", "steer"]
    task_id: str | None = None
    task_ids: list[str] | None = None
    parallel_group_id: str | None = None
    block: bool = False
    timeout: int = 60000
    full_session: bool = False
    message_limit: int = 20
    prompt: str | None = None

    @field_validator("task_id", "parallel_group_id", "prompt", mode="after")
    @classmethod
    def _strip_non_empty_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("string arguments must be non-empty")
        return stripped

    @field_validator("task_ids", mode="after")
    @classmethod
    def _normalize_task_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("task_ids must contain at least one task id")
        if len(value) > 100:
            raise ValueError("task_ids may contain at most 100 ids")
        normalized: list[str] = []
        for index, item in enumerate(value):
            stripped = item.strip()
            if not stripped:
                raise ValueError(f"task_ids[{index}] must be a non-empty string")
            normalized.append(stripped)
        if len(set(normalized)) != len(normalized):
            raise ValueError("task_ids must not contain duplicates")
        return normalized

    @field_validator("timeout", mode="after")
    @classmethod
    def _validate_timeout(cls, value: int) -> int:
        if value < 0:
            raise ValueError("timeout must be a non-negative integer number of milliseconds")
        return value

    @field_validator("message_limit", mode="after")
    @classmethod
    def _validate_message_limit(cls, value: int) -> int:
        return min(max(value, 1), 100)

    @model_validator(mode="after")
    def _validate_operation_arguments(self) -> _TaskControlArgs:
        values_by_operation = {
            "output": {"task_id", "task_ids", "parallel_group_id", "block", "timeout", "full_session", "message_limit"},
            "cancel": {"task_id"},
            "ps": set(),
            "steer": {"task_id", "prompt"},
        }
        provided = {
            name
            for name in ("task_id", "task_ids", "parallel_group_id", "block", "timeout", "full_session", "message_limit", "prompt")
            if name in self.model_fields_set
        }
        unexpected = provided - values_by_operation[self.operation]
        if unexpected:
            names = ", ".join(sorted(unexpected))
            raise ValueError(f"operation={self.operation} does not accept: {names}")

        if self.operation == "output":
            selectors = (self.task_id, self.task_ids, self.parallel_group_id)
            if sum(selector is not None for selector in selectors) != 1:
                raise ValueError("output requires exactly one of task_id, task_ids, or parallel_group_id")
            if self.full_session and self.task_id is None:
                raise ValueError("full_session is only supported with a single task_id output")
            if self.block and self.timeout < 1000:
                raise ValueError("timeout must be at least 1000 milliseconds when block=true; wait for the completion reminder instead of polling")
        elif self.operation == "cancel":
            if self.task_id is None:
                raise ValueError("cancel requires task_id")
        elif self.operation == "steer":
            if self.task_id is None or self.prompt is None:
                raise ValueError("steer requires task_id and prompt")
        return self


_OUTPUT_PROPERTIES: dict[str, object] = {
    "operation": {"const": "output"},
    "task_id": {"type": "string", "minLength": 1},
    "task_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 100, "uniqueItems": True},
    "parallel_group_id": {"type": "string", "minLength": 1},
    "block": {"type": "boolean"},
    "timeout": {
        "type": "integer",
        "minimum": 0,
        "description": "Milliseconds for block=true only; block=true requires at least 1000ms. Ignored for non-blocking reads.",
    },
    "full_session": {
        "type": "boolean",
        "description": "Include bounded child-session metadata and transcript preview; only true with a single task_id.",
    },
    "message_limit": {
        "type": "integer",
        "description": "Maximum bounded transcript events for full_session; clamped to 1-100.",
    },
}


class TaskControlTool:
    """Unified model-facing control surface for runtime-owned tasks."""

    definition = ToolDefinition(
        name="task",
        description=(
            "Control a runtime-owned delegated task with operation output, cancel, ps, or steer. "
            "Output reads one task or a bounded task group; ps lists the parent's bounded roster; "
            "cancel requests cancellation; steer dispatches the next turn for a keep-alive task."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operation": {"type": "string", "enum": ["output", "cancel", "ps", "steer"]},
                "task_id": {"type": "string", "minLength": 1},
                "task_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 100, "uniqueItems": True},
                "parallel_group_id": {"type": "string", "minLength": 1},
                "block": {"type": "boolean"},
                "timeout": {"type": "integer", "minimum": 0},
                "full_session": {"type": "boolean"},
                "message_limit": {"type": "integer"},
                "prompt": {"type": "string", "minLength": 1},
            },
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": _OUTPUT_PROPERTIES,
                    "required": ["operation"],
                    "oneOf": [
                        {"required": ["task_id"]},
                        {"required": ["task_ids"]},
                        {"required": ["parallel_group_id"]},
                    ],
                    "allOf": [
                        {
                            "if": {"required": ["full_session"], "properties": {"full_session": {"const": True}}},
                            "then": {"required": ["task_id"]},
                        },
                        {
                            "if": {"required": ["block"], "properties": {"block": {"const": True}}},
                            "then": {"properties": {"timeout": {"type": "integer", "minimum": 1000}}},
                        },
                    ],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"operation": {"const": "cancel"}, "task_id": {"type": "string", "minLength": 1}},
                    "required": ["operation", "task_id"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"operation": {"const": "ps"}},
                    "required": ["operation"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "operation": {"const": "steer"},
                        "task_id": {"type": "string", "minLength": 1},
                        "prompt": {"type": "string", "minLength": 1},
                    },
                    "required": ["operation", "task_id", "prompt"],
                },
            ],
        },
        # The operation classifier supplies read/write/execute semantics before
        # invocation; this definition must therefore not advertise the whole
        # facade as read-only.
        read_only=False,
    )

    def __init__(self, *, runtime: TaskControlRuntime) -> None:
        self._output = TaskOutputTool(runtime=runtime)
        self._cancel = TaskCancelTool(runtime=runtime)
        self._ps = TaskPsTool(runtime=runtime)
        self._steer = TaskSteerTool(runtime=runtime)

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = _TaskControlArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        if args.operation == "output":
            delegate_call = ToolCall(
                tool_name="task_output",
                arguments=args.model_dump(exclude_none=True),
                tool_call_id=call.tool_call_id,
            )
            result = self._output.invoke(delegate_call, workspace=workspace)
        elif args.operation == "cancel":
            assert args.task_id is not None
            result = self._cancel.invoke(
                ToolCall(
                    tool_name="task_cancel",
                    arguments={"taskId": args.task_id},
                ),
                workspace=workspace,
            )
        elif args.operation == "ps":
            result = self._ps.invoke(
                ToolCall(tool_name="task_ps", arguments={}, tool_call_id=call.tool_call_id),
                workspace=workspace,
            )
        else:
            assert args.task_id is not None and args.prompt is not None
            result = self._steer.invoke(
                ToolCall(
                    tool_name="task_steer",
                    arguments={"task_id": args.task_id, "prompt": args.prompt},
                    tool_call_id=call.tool_call_id,
                ),
                workspace=workspace,
            )
        return replace(
            result,
            tool_name=self.definition.name,
            content=cast(str | None, _rewrite_background_reference(result.content)),
            data=cast(dict[str, object], _rewrite_background_reference(result.data)),
            error=cast(str | None, _rewrite_background_reference(result.error)),
        )


__all__ = ["TaskControlRuntime", "TaskControlTool"]
