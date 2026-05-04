from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.task import BackgroundTaskState
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult


class BackgroundRetryRuntime(Protocol):
    def retry_background_task(self, task_id: str) -> BackgroundTaskState: ...


class _BackgroundRetryArgs(BaseModel):
    task_id: str

    @field_validator("task_id", mode="after")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("task_id must be a non-empty string")
        return stripped


class BackgroundRetryTool:
    definition = ToolDefinition(
        name="background_retry",
        description=(
            "Retry a failed, cancelled, or interrupted background task by creating a fresh "
            "queued task with the original delegated request."
        ),
        input_schema={"task_id": {"type": "string"}},
        read_only=True,
    )

    def __init__(self, *, runtime: BackgroundRetryRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _BackgroundRetryArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        try:
            task = self._runtime.retry_background_task(args.task_id)
        except ValueError as exc:
            message = str(exc)
            if "unknown background task" not in message:
                raise
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=f"Background task {args.task_id}: unknown ({message})",
                data={
                    "task_id": args.task_id,
                    "retry_task_id": None,
                    "status": "unknown",
                    "session_id": None,
                    "parent_session_id": None,
                    "error": message,
                    "terminal": True,
                    "retry_started": False,
                },
            )

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=(
                f"Retried background task {args.task_id} as {task.task.id}: {task.status}. "
                f"Use background_output(task_id='{task.task.id}') to inspect the retry."
            ),
            data={
                "task_id": args.task_id,
                "retry_task_id": task.task.id,
                "status": task.status,
                "session_id": task.session_id,
                "parent_session_id": task.parent_session_id,
                "error": task.error,
                "terminal": False,
                "retry_started": True,
                "retrieval_instruction": f'background_output(task_id="{task.task.id}")',
            },
        )
