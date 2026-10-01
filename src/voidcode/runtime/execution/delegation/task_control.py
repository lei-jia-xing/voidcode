from __future__ import annotations

from dataclasses import replace
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import MessageLimit, TimeoutMs, parse_tool_args
from ....tools.contracts import ToolCall, ToolResult
from .task_cancel import TaskCancelRuntime, TaskCancelTool
from .task_output import TaskOutputRuntime, TaskOutputTool
from .task_ps import TaskPsRuntime, TaskPsTool
from .task_steer import TaskSteerRuntime, TaskSteerTool


class TaskControlRuntime(TaskOutputRuntime, TaskCancelRuntime, TaskPsRuntime, TaskSteerRuntime, Protocol):
    """Runtime authority used by the unified task control facade."""


class _TaskControlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Literal["output", "cancel", "ps", "steer"]
    task_id: str | None = None
    task_ids: list[str] | None = None
    parallel_group_id: str | None = None
    block: bool = False
    timeout: TimeoutMs = 60000
    full_session: bool = False
    message_limit: MessageLimit = 20
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


class TaskControlTool:
    """Unified model-facing control surface for runtime-owned tasks."""

    name = "task"

    def __init__(self, *, runtime: TaskControlRuntime) -> None:
        self._output = TaskOutputTool(runtime=runtime)
        self._cancel = TaskCancelTool(runtime=runtime)
        self._ps = TaskPsTool(runtime=runtime)
        self._steer = TaskSteerTool(runtime=runtime)

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        args = parse_tool_args(_TaskControlArgs, call.arguments, tool_name=self.name)

        if args.operation == "output":
            delegate_call = ToolCall(
                tool_name="task_output",
                arguments=args.model_dump(exclude_none=True),
                tool_call_id=call.tool_call_id,
            )
            result = self._output.invoke(delegate_call, context=context)
        elif args.operation == "cancel":
            assert args.task_id is not None
            result = self._cancel.invoke(
                ToolCall(
                    tool_name="task_cancel",
                    arguments={"taskId": args.task_id},
                ),
                context=context,
            )
        elif args.operation == "ps":
            result = self._ps.invoke(ToolCall(tool_name="task_ps", arguments={}, tool_call_id=call.tool_call_id), context=context)
        else:
            assert args.task_id is not None and args.prompt is not None
            result = self._steer.invoke(
                ToolCall(
                    tool_name="task_steer",
                    arguments={"task_id": args.task_id, "prompt": args.prompt},
                    tool_call_id=call.tool_call_id,
                ),
                context=context,
            )
        return replace(result, tool_name=self.name)


__all__ = ["TaskControlRuntime", "TaskControlTool"]
