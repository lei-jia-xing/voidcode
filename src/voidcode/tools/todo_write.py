from __future__ import annotations

from pathlib import Path
from typing import ClassVar, cast

from .contracts import ToolCall, ToolDefinition, ToolResult


class TodoWriteTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="todowrite",
        description="Manage todo list with status and priority.",
        input_schema={
            "todos": {"type": "array", "description": "Array of {content, status, priority}"},
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        todos_value = call.arguments.get("todos", [])
        todos: list[object] = []
        if isinstance(todos_value, list):
            todos = cast(list[object], todos_value)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Updated {len(todos)} todos",
        )
