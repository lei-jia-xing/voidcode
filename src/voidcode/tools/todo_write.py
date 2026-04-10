from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, cast

from .contracts import ToolCall, ToolDefinition, ToolResult


class TodoWriteTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="todo_write",
        description="Manage todo list with status and priority.",
        input_schema={
            "todos": {"type": "array", "description": "Array of {content, status, priority}"},
        },
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        todos_value = call.arguments.get("todos", [])
        todos: list[object] = []
        if isinstance(todos_value, list):
            todos = cast(list[object], todos_value)
        else:
            raise ValueError("todo_write requires todos array")

        allowed_status = {"pending", "in_progress", "completed", "cancelled"}
        allowed_priority = {"high", "medium", "low"}

        normalized: list[dict[str, str]] = []
        for idx, item in enumerate(todos, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"todo_write item #{idx} must be an object")
            item_dict = cast(dict[str, Any], item)

            content = item_dict.get("content")
            status = item_dict.get("status")
            priority = item_dict.get("priority")

            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"todo_write item #{idx} content must be non-empty string")
            if not isinstance(status, str) or status not in allowed_status:
                raise ValueError(f"todo_write item #{idx} invalid status: {status}")
            if not isinstance(priority, str) or priority not in allowed_priority:
                raise ValueError(f"todo_write item #{idx} invalid priority: {priority}")

            normalized.append({"content": content.strip(), "status": status, "priority": priority})

        store_dir = workspace / ".voidcode"
        store_dir.mkdir(parents=True, exist_ok=True)
        store_path = store_dir / "todos.json"
        store_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        summary = {
            "total": len(normalized),
            "pending": sum(1 for t in normalized if t["status"] == "pending"),
            "in_progress": sum(1 for t in normalized if t["status"] == "in_progress"),
            "completed": sum(1 for t in normalized if t["status"] == "completed"),
            "cancelled": sum(1 for t in normalized if t["status"] == "cancelled"),
        }

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Updated {len(normalized)} todos",
            data={
                "path": store_path.relative_to(workspace.resolve()).as_posix(),
                "summary": summary,
            },
        )
