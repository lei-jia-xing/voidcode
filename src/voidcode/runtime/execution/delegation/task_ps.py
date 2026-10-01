from __future__ import annotations

from typing import Protocol

from ....core.tool_context import ToolContext
from ....tools.contracts import ToolCall, ToolResult


class TaskPsRuntime(Protocol):
    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]: ...


class TaskPsTool:
    name = "task_ps"

    def __init__(self, *, runtime: TaskPsRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        caller_session_id = context.require_session_id()
        if call.arguments:
            raise ValueError('task(operation="ps") accepts no arguments')
        payload = self._runtime.background_task_roster(parent_session_id=caller_session_id)
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
