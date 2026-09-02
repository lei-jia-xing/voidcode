from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import current_runtime_tool_context


class BackgroundPsRuntime(Protocol):
    def background_task_roster(self, *, parent_session_id: str) -> dict[str, object]: ...


class BackgroundPsTool:
    """Expose the runtime-owned, bounded background-task roster to the model."""

    definition = ToolDefinition(
        name="background_ps",
        description=(
            "List the active session's background task roster as a bounded status projection. "
            "Runtime ownership is enforced; no prompts or transcripts are returned."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        read_only=True,
    )

    def __init__(self, *, runtime: BackgroundPsRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        if call.arguments:
            raise ValueError("background_ps accepts no arguments")
        context = current_runtime_tool_context()
        if context is None:
            raise RuntimeError("background_ps requires an active runtime tool invocation context")
        payload = self._runtime.background_task_roster(parent_session_id=context.session_id)
        tasks = payload.get("tasks")
        task_count = len(tasks) if isinstance(tasks, list) else 0
        content = f"Background task roster: {task_count} task(s)"
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=payload,
        )


__all__ = ["BackgroundPsRuntime", "BackgroundPsTool"]
