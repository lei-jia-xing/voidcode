from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..contracts import ToolCall, ToolResult
from ..runtime_context import current_runtime_tool_context


class TaskPsRuntime(Protocol):
    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]: ...


class TaskPsTool:
    name = "task_ps"

    def __init__(self, *, runtime: TaskPsRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        if call.arguments:
            raise ValueError('task(operation="ps") accepts no arguments')
        context = current_runtime_tool_context()
        if context is None:
            raise RuntimeError('task(operation="ps") requires an active runtime tool invocation context')
        payload = self._runtime.background_task_roster(parent_session_id=context.session_id)
        tasks = payload.get("tasks")
        task_count = len(tasks) if isinstance(tasks, list) else 0
        content = f"Background task roster: {task_count} task(s)"
        return ToolResult(
            tool_name=self.name,
            status="ok",
            content=content,
            data=payload,
        )


__all__ = ["TaskPsRuntime", "TaskPsTool"]
