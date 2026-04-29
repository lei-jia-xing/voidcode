from __future__ import annotations

from typing import Literal, TypedDict, cast

type TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]
type TodoPriority = Literal["high", "medium", "low"]

TODO_STATUSES: tuple[TodoStatus, ...] = ("pending", "in_progress", "completed", "cancelled")
TODO_PRIORITIES: tuple[TodoPriority, ...] = ("high", "medium", "low")


class RuntimeTodoItem(TypedDict):
    content: str
    status: TodoStatus
    priority: TodoPriority
    position: int
    updated_at: int


class RuntimeTodoSummary(TypedDict):
    total: int
    pending: int
    in_progress: int
    completed: int
    cancelled: int
    active: int


def todo_summary(todos: tuple[RuntimeTodoItem, ...]) -> RuntimeTodoSummary:
    pending = sum(1 for todo in todos if todo["status"] == "pending")
    in_progress = sum(1 for todo in todos if todo["status"] == "in_progress")
    completed = sum(1 for todo in todos if todo["status"] == "completed")
    cancelled = sum(1 for todo in todos if todo["status"] == "cancelled")
    return {
        "total": len(todos),
        "pending": pending,
        "in_progress": in_progress,
        "completed": completed,
        "cancelled": cancelled,
        "active": pending + in_progress,
    }


def runtime_todos_from_tool_payload(
    raw_todos: object,
    *,
    updated_at: int,
) -> tuple[RuntimeTodoItem, ...]:
    if not isinstance(raw_todos, list):
        return ()
    todos: list[RuntimeTodoItem] = []
    for position, raw_item in enumerate(cast(list[object], raw_todos), start=1):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[object, object], raw_item)
        content = item.get("content")
        status = item.get("status")
        priority = item.get("priority")
        if not isinstance(content, str) or not content.strip():
            continue
        if status not in TODO_STATUSES or priority not in TODO_PRIORITIES:
            continue
        todos.append(
            {
                "content": content.strip(),
                "status": status,
                "priority": priority,
                "position": position,
                "updated_at": updated_at,
            }
        )
    return tuple(todos)


def runtime_todos_from_state_payload(raw_todos: object) -> tuple[RuntimeTodoItem, ...]:
    if not isinstance(raw_todos, list):
        return ()
    todos: list[RuntimeTodoItem] = []
    for fallback_position, raw_item in enumerate(cast(list[object], raw_todos), start=1):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[object, object], raw_item)
        content = item.get("content")
        status = item.get("status")
        priority = item.get("priority")
        raw_position = item.get("position")
        raw_updated_at = item.get("updated_at")
        if not isinstance(content, str) or not content.strip():
            continue
        if status not in TODO_STATUSES or priority not in TODO_PRIORITIES:
            continue
        position = (
            raw_position
            if isinstance(raw_position, int) and raw_position > 0
            else fallback_position
        )
        updated_at = (
            raw_updated_at if isinstance(raw_updated_at, int) and raw_updated_at >= 0 else 0
        )
        todos.append(
            {
                "content": content.strip(),
                "status": status,
                "priority": priority,
                "position": position,
                "updated_at": updated_at,
            }
        )
    return tuple(sorted(todos, key=lambda todo: todo["position"]))


def todo_state_payload(
    todos: tuple[RuntimeTodoItem, ...],
    *,
    revision: int,
) -> dict[str, object]:
    return {
        "version": 1,
        "revision": revision,
        "todos": [dict(todo) for todo in todos],
        "summary": todo_summary(todos),
    }


def todo_event_payload(
    *,
    session_id: str,
    todos: tuple[RuntimeTodoItem, ...],
    revision: int,
) -> dict[str, object]:
    summary = todo_summary(todos)
    return {
        "session_id": session_id,
        "todo_count": summary["total"],
        "active_count": summary["active"],
        "pending_count": summary["pending"],
        "in_progress_count": summary["in_progress"],
        "completed_count": summary["completed"],
        "cancelled_count": summary["cancelled"],
        "revision": revision,
        "todos": [dict(todo) for todo in todos],
        "summary": dict(summary),
    }


def todo_state_from_session_metadata(
    session_metadata: dict[str, object],
) -> dict[str, object] | None:
    raw_runtime_state = session_metadata.get("runtime_state")
    if not isinstance(raw_runtime_state, dict):
        return None
    runtime_state = cast(dict[str, object], raw_runtime_state)
    raw_todo_state = runtime_state.get("todos")
    if not isinstance(raw_todo_state, dict):
        return None
    todo_state = cast(dict[str, object], raw_todo_state)
    todos = runtime_todos_from_state_payload(todo_state.get("todos"))
    revision = todo_state.get("revision")
    return todo_state_payload(
        todos,
        revision=revision if isinstance(revision, int) and revision >= 0 else 0,
    )


def render_provider_todo_state(session_metadata: dict[str, object]) -> str | None:
    todo_state = todo_state_from_session_metadata(session_metadata)
    if todo_state is None:
        return None
    todos = runtime_todos_from_state_payload(todo_state.get("todos"))
    active_todos = tuple(todo for todo in todos if todo["status"] in {"pending", "in_progress"})
    if not active_todos:
        return None
    lines = [
        "Runtime-managed todo state is active for this session.",
        "Use this as the current plan truth; do not recreate it from older tool results.",
        "Current active todos:",
    ]
    for todo in active_todos:
        lines.append(f"{todo['position']}. [{todo['status']}/{todo['priority']}] {todo['content']}")
    return "\n".join(lines)
