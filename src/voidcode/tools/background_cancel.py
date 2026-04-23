from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, model_validator

from ..runtime.task import BackgroundTaskState
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
        task = self._runtime.cancel_background_task(args.taskId)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Cancelled background task {task.task.id}: {task.status}",
            data={
                "task_id": task.task.id,
                "status": task.status,
                "session_id": task.session_id,
                "parent_session_id": task.parent_session_id,
                "error": task.error,
            },
        )
