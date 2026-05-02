from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.task import BackgroundTaskState, is_background_task_terminal
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult


class BackgroundCancelRuntime(Protocol):
    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...


class _BackgroundCancelArgs(BaseModel):
    taskId: str | None = None
    all: bool = False

    @field_validator("taskId", mode="after")
    @classmethod
    def _validate_task_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("taskId must be a non-empty string when provided")
        return stripped


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
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc
        if args.all:
            raise ValueError(
                "background_cancel Validation error: all: Value error, "
                "all=true is not supported in VoidCode yet (received bool). "
                "Please retry with corrected arguments that satisfy the tool schema."
            )
        if args.taskId is None:
            raise ValueError(
                "background_cancel Validation error: taskId: Value error, "
                "taskId is required when all is false (received NoneType). "
                "Please retry with corrected arguments that satisfy the tool schema."
            )
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
