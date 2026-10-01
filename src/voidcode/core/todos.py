from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, TypedDict, TypeIs

TodoStatus = Literal["pending", "in_progress", "completed", "abandoned", "blocked"]
TODO_STATUSES: tuple[TodoStatus, ...] = (
    "pending",
    "in_progress",
    "completed",
    "abandoned",
    "blocked",
)


class TodoTask(TypedDict, total=False):
    content: str
    status: TodoStatus
    blocker: str


class TodoPhase(TypedDict):
    name: str
    tasks: list[TodoTask]


class TodoSummary(TypedDict):
    total: int
    pending: int
    in_progress: int
    completed: int
    abandoned: int
    blocked: int
    active: int


def is_todo_status(value: object) -> TypeIs[TodoStatus]:
    return value in TODO_STATUSES


def todo_summary(phases: Iterable[TodoPhase]) -> TodoSummary:
    counts = {status: 0 for status in TODO_STATUSES}
    for phase in phases:
        for task in phase["tasks"]:
            counts[task["status"]] += 1
    return {
        "total": sum(counts.values()),
        "pending": counts["pending"],
        "in_progress": counts["in_progress"],
        "completed": counts["completed"],
        "abandoned": counts["abandoned"],
        "blocked": counts["blocked"],
        "active": counts["pending"] + counts["in_progress"],
    }
