from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, field_validator

from ....core.tool_context import ToolContext
from ....tools._pydantic_args import parse_tool_args, validate_non_empty_stripped
from ....tools.contracts import ToolCall, ToolResult
from ...background.models import BackgroundTaskState, is_background_task_terminal
from ...contracts import UnknownBackgroundTaskError


class TaskCancelRuntime(Protocol):
    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...


def _unknown_task_result(task_id: str, message: str) -> ToolResult:
    return ToolResult(
        tool_name="task_cancel",
        status="ok",
        content=f"Background task {task_id}: unknown ({message})",
        data={
            "task_id": task_id,
            "status": "unknown",
            "session_id": None,
            "parent_session_id": None,
            "error": message,
            "cancellation_cause": "unknown background task",
            "cancel_requested": False,
            "terminal": True,
        },
    )


class _TaskCancelArgs(BaseModel):
    taskId: str

    _validate_task_id = field_validator("taskId", mode="after")(validate_non_empty_stripped)


class TaskCancelTool:
    name = "task_cancel"

    def __init__(self, *, runtime: TaskCancelRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        caller_session_id = context.require_session_id()
        args = parse_tool_args(_TaskCancelArgs, call.arguments, tool_name=self.name)
        try:
            self._runtime.authorize_background_task_owner(
                args.taskId,
                parent_session_id=caller_session_id,
            )
        except UnknownBackgroundTaskError as exc:
            return _unknown_task_result(args.taskId, str(exc))
        try:
            task = self._runtime.cancel_background_task(args.taskId)
        except UnknownBackgroundTaskError as exc:
            return _unknown_task_result(args.taskId, str(exc))
        cause = task.cancellation_cause or task.error
        if task.status == "cancelled":
            content = f"Cancelled background task {task.task.id}: {cause or 'cancelled'}"
        elif task.status == "running" and task.cancel_requested_at is not None:
            content = f"Cancellation requested for background task {task.task.id}"
        elif is_background_task_terminal(task.status):
            content = f"Background task {task.task.id} is already {task.status}"
        else:
            content = f"Background task {task.task.id}: {task.status}"
        return ToolResult(
            tool_name=self.name,
            status="ok",
            content=content,
            data={
                "task_id": task.task.id,
                "status": task.status,
                "session_id": task.session_id,
                "parent_session_id": task.parent_session_id,
                "error": task.error,
                "cancellation_cause": cause,
                "cancel_requested": task.cancel_requested_at is not None,
                "terminal": is_background_task_terminal(task.status),
            },
        )


__all__ = ["TaskCancelRuntime", "TaskCancelTool"]
