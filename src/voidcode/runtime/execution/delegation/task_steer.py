from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, field_validator

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import NonEmptyPrompt, parse_tool_args, validate_non_empty_stripped
from ....tools.contracts import ToolCall, ToolResult
from ...background.models import BackgroundTaskState, is_background_task_terminal


class TaskSteerRuntime(Protocol):
    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState: ...


class _TaskSteerArgs(BaseModel):
    task_id: str
    prompt: NonEmptyPrompt

    _validate_task_id = field_validator("task_id", mode="after")(validate_non_empty_stripped)


class TaskSteerTool:
    name = "task_steer"

    def __init__(self, *, runtime: TaskSteerRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        caller_session_id = context.require_session_id()
        args = parse_tool_args(_TaskSteerArgs, call.arguments, tool_name=self.name)

        self._runtime.authorize_background_task_owner(
            args.task_id,
            parent_session_id=caller_session_id,
        )
        current_task = self._runtime.load_background_task(args.task_id)
        if current_task.parent_session_id != caller_session_id:
            raise ValueError(
                f"steer_task cannot steer background task {args.task_id}: only its parent "
                f"session ({current_task.parent_session_id or 'unknown'}) may steer it "
                f"(current session: {context.session_id})"
            )
        task = self._runtime.steer_background_task(args.task_id, args.prompt)
        waiting_reason = task.observability.waiting_reason if task.observability is not None else None
        if task.status == "running":
            content = (
                f"Dispatched steer for background task {task.task.id} (status: running). "
                "The worker will park as idle (awaiting_steer) after this turn unless it "
                "submits its final result, which completes the task."
            )
        else:
            content = f"Background task {task.task.id} after steer: {task.status}"
        return ToolResult(
            tool_name=self.name,
            status="ok",
            content=content,
            data={
                "task_id": task.task.id,
                "status": task.status,
                "parent_session_id": task.parent_session_id,
                "child_session_id": task.session_id,
                "keep_alive": task.keep_alive,
                "steer_prompt": task.steer_prompt,
                "waiting_reason": waiting_reason,
                "terminal": is_background_task_terminal(task.status),
            },
        )


__all__ = ["TaskSteerRuntime", "TaskSteerTool"]
