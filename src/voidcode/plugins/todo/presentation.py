"""Interactive list presentation and per-task operation choices."""

from __future__ import annotations

import json
from collections.abc import Mapping

from voidcode.sdk.commands import CommandPresentation
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

from . import text
from .models import TERMINAL_STATUSES, TodoTask
from .operations import TodoOp
from .state import TodoState


def task_from_id(state: TodoState, identifier: object) -> TodoTask:
    if not isinstance(identifier, str):
        raise ValueError("Choose a todo task")
    task = next((task for phase in state.phases for task in phase["tasks"] if task["id"] == identifier), None)
    if task is None:
        raise ValueError("Todo task is no longer available; reopen /todo")
    return task


def paged_phase_payload(state: TodoState, *, offset: int, limit: int) -> list[dict[str, object]]:
    """The displayed page as phase-shaped receipt data, never the whole list."""
    selected: dict[int, list[TodoTask]] = {}
    index = 0
    for phase_index, phase in enumerate(state.phases):
        for task in phase["tasks"]:
            if offset <= index < offset + limit:
                selected.setdefault(phase_index, []).append(task)
            index += 1
    return [{"name": state.phases[phase_index]["name"], "tasks": [dict(task) for task in tasks]} for phase_index, tasks in selected.items()]


def duplicate_contents(state: TodoState) -> set[str]:
    counts: dict[str, int] = {}
    for phase in state.phases:
        for task in phase["tasks"]:
            counts[task["content"]] = counts.get(task["content"], 0) + 1
    return {content for content, count in counts.items() if count > 1}


def _edit_label(task: TodoTask, duplicates: set[str]) -> str:
    label = f"Edit: {text.display_text(task['content'], 80)}"
    return f"{label} [{task['id'][:8]}]" if task["content"] in duplicates else label


def picker_label(task: TodoTask, duplicates: set[str]) -> str:
    label = text.display_text(task["content"], 160)
    suffix = f" [{task['id'][:8]}]" if task["content"] in duplicates else ""
    return f"{label}{suffix} [{task['status']}]"


def todo_presentation(state: TodoState, *, offset: int = 0, limit: int = 10) -> CommandPresentation:
    tasks = [(phase["name"], task) for phase in state.phases for task in phase["tasks"]]
    duplicates = duplicate_contents(state)
    count = min(limit, max(0, len(tasks) - offset))
    while True:
        selected = tasks[offset : offset + count]
        lines = [f"Tasks {offset}–{offset + len(selected)} of {len(tasks)}", ""]
        for phase, task in selected:
            blocker = f" — {text.display_text(task['blocker'], 120)}" if task.get("blocker") else ""
            preview = text.display_text(task["content"], 400) + ("…" if text.utf8_len(task["content"]) > 400 else "")
            lines.append(f"- **{text.display_text(phase, 80)}** · [{task['status']}] {preview}{blocker}")
        if not tasks:
            lines.append("Todo list is empty. Add tasks to start planning.")
        actions: list[dict[str, object]] = [{"label": "Add tasks", "action": "todo.add", "prepare_input": True}]
        if offset:
            actions.append({"label": "Previous tasks", "action": "todo", "arguments": {"offset": max(0, offset - (count or limit)), "limit": limit}})
        if offset + count < len(tasks):
            actions.append({"label": "Next tasks", "action": "todo", "arguments": {"offset": offset + count, "limit": limit}})
        actions.extend(
            {"label": _edit_label(task, duplicates), "action": "todo.edit", "arguments": {"task_id": task["id"]}} for _phase, task in selected
        )
        data = {"content": "\n".join(lines), "actions": actions}
        if len(json.dumps(data).encode("utf-8")) <= MAX_VIEW_DATA_BYTES - 4096:
            return CommandPresentation(title="Todo list", view_type="markdown", data=data)
        if count <= 1:
            raise ValueError("Todo detail exceeds the view limit")
        count = max(1, count // 2)


_TASK_OP_LABELS: Mapping[TodoOp, str] = {
    TodoOp.START: "Start task",
    TodoOp.DONE: "Mark completed",
    TodoOp.DROP: "Abandon task",
    TodoOp.BLOCK: "Block task",
    TodoOp.UNBLOCK: "Unblock task",
    TodoOp.RM: "Remove task",
}


def task_operations(task: TodoTask) -> list[dict[str, str]]:
    ops: tuple[TodoOp, ...]
    if task["status"] in TERMINAL_STATUSES:
        ops = (TodoOp.RM,)
    elif task["status"] == "blocked":
        ops = (TodoOp.UNBLOCK, TodoOp.DONE, TodoOp.DROP, TodoOp.RM)
    else:
        ops = (TodoOp.DONE, TodoOp.DROP, TodoOp.RM)
        ops = (TodoOp.BLOCK, *ops)
        if task["status"] != "in_progress":
            ops = (TodoOp.START, *ops)
    return [{"id": op.value, "label": _TASK_OP_LABELS[op]} for op in ops]
