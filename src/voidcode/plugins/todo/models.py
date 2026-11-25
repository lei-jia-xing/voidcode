"""Typed todo values shared by state, operations, rendering, and reminders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

TodoStatus = Literal["pending", "in_progress", "completed", "abandoned", "blocked"]
TODO_STATUSES: tuple[TodoStatus, ...] = ("pending", "in_progress", "completed", "abandoned", "blocked")

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "abandoned"})
INCOMPLETE_STATUSES: frozenset[str] = frozenset({"pending", "in_progress"})
ACTIVE_PROVIDER_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "blocked"})

TODO_REMINDER_KIND: Literal["todo"] = "todo"
TODO_MID_RUN_KIND: Literal["todo_mid_run"] = "todo_mid_run"
ReminderKind = Literal["todo", "todo_mid_run"]


class TodoTask(TypedDict):
    id: str
    content: str
    status: TodoStatus
    blocker: NotRequired[str]


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


def is_todo_status(value: object) -> bool:
    return isinstance(value, str) and value in TODO_STATUSES


def todo_summary(phases: Sequence[TodoPhase]) -> TodoSummary:
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
        "active": counts["pending"] + counts["in_progress"] + counts["blocked"],
    }


@dataclass(frozen=True, slots=True)
class ReminderPolicy:
    """Per-kind reminder policy plus its per-cycle bookkeeping, fused.

    Completion reminders (``kind == "todo"``) use attempts/awaiting_progress/
    progress_watermark; mid-run nudges use attempts/mutations/mutation_baseline.
    Both stamp cycle_run_id. The two instances in TodoState keep independent
    budgets; only the shape and the decision/text entries are shared.
    """

    kind: ReminderKind
    attempts: int = 0
    awaiting_progress: bool = False
    progress_watermark: int = 0
    mutations: int = 0
    mutation_baseline: int = 0
    cycle_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReminderDecision[S]:
    """One verdict from either reminder kind, plus the state to persist for it."""

    state: S
    text: str | None = None

    @property
    def injects(self) -> bool:
        return self.text is not None
