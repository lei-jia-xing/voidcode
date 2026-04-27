from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ValidationError, field_validator

from .contracts import ToolCall, ToolDefinition, ToolResult


class _TodoItemModel(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]
    priority: Literal["high", "medium", "low"]

    @field_validator("content", mode="after")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content must be non-empty string")
        return stripped


def _parse_todo_item(item: object, *, idx: int) -> dict[str, str]:
    try:
        parsed = _TodoItemModel.model_validate(item)
    except ValidationError as exc:
        first_error = exc.errors()[0]
        location = first_error.get("loc", ())
        field_name = location[0] if location else None

        if field_name is None:
            raise ValueError(f"todo_write item #{idx} must be an object") from exc

        item_payload: dict[str, object]
        if isinstance(item, dict):
            item_payload = cast(dict[str, object], item)
        else:
            item_payload = {}

        if field_name == "content":
            raise ValueError(f"todo_write item #{idx} content must be non-empty string") from exc
        if field_name == "status":
            raise ValueError(
                f"todo_write item #{idx} invalid status: {item_payload.get('status')}"
            ) from exc
        if field_name == "priority":
            raise ValueError(
                f"todo_write item #{idx} invalid priority: {item_payload.get('priority')}"
            ) from exc

        raise ValueError(f"todo_write item #{idx} must be an object") from exc

    return {
        "content": parsed.content,
        "status": parsed.status,
        "priority": parsed.priority,
    }


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
        _ = workspace
        todos_value = call.arguments.get("todos", [])
        if not isinstance(todos_value, list):
            raise ValueError("todo_write requires todos array")
        todos = cast(list[object], todos_value)

        normalized: list[dict[str, str]] = []
        for idx, item in enumerate(todos, start=1):
            normalized.append(_parse_todo_item(item, idx=idx))

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
                "todos": normalized,
                "summary": summary,
            },
        )
