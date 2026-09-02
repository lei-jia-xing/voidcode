from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.task import BackgroundTaskState, is_background_task_terminal
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context


class BackgroundCancelRuntime(Protocol):
    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...


def _unknown_task_result(task_id: str, message: str) -> ToolResult:
    return ToolResult(
        tool_name="background_cancel",
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


class _BackgroundCancelArgs(BaseModel):
    taskId: str

    @field_validator("taskId", mode="after")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("taskId must be a non-empty string")
        return stripped


class BackgroundCancelTool:
    definition = ToolDefinition(
        name="background_cancel",
        description="Cancel a running background task by id.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"taskId": {"type": "string", "minLength": 1}},
            "required": ["taskId"],
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
        context = current_runtime_tool_context()
        if context is not None:
            try:
                self._runtime.authorize_background_task_owner(
                    args.taskId,
                    parent_session_id=context.session_id,
                )
            except ValueError as exc:
                # An unknown id has no task data to disclose. Preserve the
                # stable unknown-task result while denying existing foreign ids.
                message = str(exc)
                if "unknown background task" not in message:
                    raise
                return _unknown_task_result(args.taskId, message)
        try:
            task = self._runtime.cancel_background_task(args.taskId)
        except ValueError as exc:
            message = str(exc)
            if "unknown background task" not in message:
                raise
            return _unknown_task_result(args.taskId, message)
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
