from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.task import BackgroundTaskState, is_background_task_terminal
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import require_runtime_tool_context


class SteerTaskRuntime(Protocol):
    def load_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState: ...


class _SteerTaskArgs(BaseModel):
    task_id: str
    prompt: str

    @field_validator("task_id", mode="after")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("task_id must be a non-empty string")
        return stripped

    @field_validator("prompt", mode="after")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must be a non-empty string")
        return stripped


class SteerTaskTool:
    definition = ToolDefinition(
        name="steer_task",
        description=(
            "Dispatch a new worker turn for a keep-alive background task created with "
            "task(keep_alive=true, run_in_background=true). The task must be idle "
            "(awaiting_steer) or interrupted (resumable breakpoint after a process "
            "restart); a task with a turn in flight cannot be steered (no pipelining). "
            "Only the task's parent session may steer it. After each steer turn the "
            "worker parks back as idle unless it submits its final result, which "
            "completes the task."
        ),
        input_schema={
            "task_id": {
                "type": "string",
                "description": "Background task id returned by the task tool.",
                "minLength": 1,
            },
            "prompt": {
                "type": "string",
                "description": "Next instruction for the keep-alive worker turn.",
                "minLength": 1,
            },
        },
        read_only=True,
    )

    def __init__(self, *, runtime: SteerTaskRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _SteerTaskArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        context = require_runtime_tool_context(self.definition.name)
        current_task = self._runtime.load_background_task(args.task_id)
        if current_task.parent_session_id != context.session_id:
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
            tool_name=self.definition.name,
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
