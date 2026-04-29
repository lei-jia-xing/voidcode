from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, model_validator

from ..runtime.task import BackgroundTaskState, is_background_task_terminal
from .contracts import ToolCall, ToolDefinition, ToolResult


class BackgroundCancelRuntime(Protocol):
    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...


class _BackgroundCancelArgs(BaseModel):
    taskId: str | None = None
    all: bool = False

    @model_validator(mode="after")
    def _validate_args(self) -> _BackgroundCancelArgs:
        if self.all:
            raise ValueError("background_cancel(all=true) is not supported in VoidCode yet")
        if self.taskId is None or not self.taskId.strip():
            raise ValueError("background_cancel requires taskId when all is false")
        self.taskId = self.taskId.strip()
        return self


class BackgroundCancelTool:
    definition = ToolDefinition(
        name="background_cancel",
        description="Cancel a running background task by id.",
        input_schema={
            "taskId": {"type": "string"},
            "all": {"type": "boolean"},
        },
        read_only=True,
    )

    def __init__(self, *, runtime: BackgroundCancelRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _BackgroundCancelArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(str(exc.errors()[0]["msg"])) from exc
        assert args.taskId is not None
        try:
            task = self._runtime.cancel_background_task(args.taskId)
        except ValueError as exc:
            message = str(exc)
            if "unknown background task" not in message:
                raise
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=f"Background task {args.taskId}: unknown ({message})",
                data={
                    "task_id": args.taskId,
                    "status": "unknown",
                    "session_id": None,
                    "parent_session_id": None,
                    "error": message,
                    "cancellation_cause": "unknown background task",
                    "cancel_requested": False,
                    "terminal": True,
                },
            )
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
            tool_name=self.definition.name,
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
