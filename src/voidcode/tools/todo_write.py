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


def _render_todo_content(todos: list[dict[str, str]]) -> str:
    if not todos:
        return "Updated 0 todos"
    lines = [f"Updated {len(todos)} todos"]
    for position, todo in enumerate(todos, start=1):
        lines.append(f"{position}. [{todo['status']}/{todo['priority']}] {todo['content']}")
    return "\n".join(lines)


_TODO_WRITE_DESCRIPTION = (
    "Manage a structured todo list for the current session. Use this tool\n"
    "proactively to track multi-step work and surface progress to the user.\n"
    "\n"
    "WHEN TO USE (mandatory triggers):\n"
    "  - The task has 2 or more distinct steps.\n"
    "  - The user provided multiple items (numbered or comma-separated).\n"
    "  - You receive new requirements that change the plan.\n"
    "  - The scope is uncertain and writing it down clarifies thinking.\n"
    "\n"
    "WHEN NOT TO USE:\n"
    "  - A single trivial task.\n"
    "  - Purely conversational/informational requests.\n"
    "  - Tasks that finish in fewer than 3 trivial steps.\n"
    "\n"
    "DISCIPLINE (non-negotiable):\n"
    "  - Mark a todo `in_progress` BEFORE starting work on it.\n"
    "  - Only ONE `in_progress` todo at a time. The runtime will reject\n"
    "    payloads that violate this constraint.\n"
    "  - Mark `completed` IMMEDIATELY after finishing; never batch.\n"
    "  - Use `cancelled` (not delete) when a todo becomes irrelevant.\n"
    "  - Each item must have content, status, and priority.\n"
    "\n"
    "ITEM SHAPE: {content: str, status: pending|in_progress|completed|cancelled,\n"
    "             priority: high|medium|low}\n"
)


class TodoWriteTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="todo_write",
        description=_TODO_WRITE_DESCRIPTION,
        input_schema={
            "todos": {
                "type": "array",
                "description": (
                    "Full replacement list of {content, status, priority} items. "
                    "At most one item may have status='in_progress'."
                ),
            },
        },
        read_only=True,
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

        in_progress_count = sum(1 for t in normalized if t["status"] == "in_progress")
        if in_progress_count > 1:
            raise ValueError(
                "todo_write rejects payloads with more than one `in_progress` todo; "
                "complete or move the current one to `pending` before starting another."
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
            content=_render_todo_content(normalized),
            data={
                "todos": normalized,
                "summary": summary,
            },
        )
