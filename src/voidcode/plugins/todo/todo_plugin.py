"""Manage session todos, context, and durable reminders."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import ClassVar, Literal, TypedDict

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import ContextContribution, InvocationFinishedFact, PluginFact, RunFinishedFact, RunRequestedFact
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
    is_read_tier,
)
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

SCHEMA_VERSION = 2
OWN_TOOL_NAMES: frozenset[str] = frozenset({"todo"})
OWNER = "todo"

TodoStatus = Literal["pending", "in_progress", "completed", "abandoned", "blocked"]
TODO_STATUSES: tuple[TodoStatus, ...] = ("pending", "in_progress", "completed", "abandoned", "blocked")
_TODO_OPS: tuple[str, ...] = ("init", "start", "done", "drop", "block", "unblock", "append", "rm", "view")
_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "abandoned"})
_INCOMPLETE_STATUSES: frozenset[str] = frozenset({"pending", "in_progress"})
_ACTIVE_PROVIDER_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "blocked"})

_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "revision", "phases", "summary", "reminders"})
_REMINDER_KEYS: frozenset[str] = frozenset({"todo", "todo_mid_run", "invocations", "cursor_sequence", "cycle_run_id"})
#: How many event pages one observation may consume while looking for a new user prompt.
_MAX_EVENT_PAGES = 3
_EVENT_PAGE_SIZE = 200


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
        "active": counts["pending"] + counts["in_progress"],
    }


# --- Reminder bookkeeping ---


@dataclass(frozen=True, slots=True)
class TodoReminderState:
    """Per-cycle completion-reminder bookkeeping."""

    attempts: int = 0
    awaiting_progress: bool = False
    progress_watermark: int = 0
    cycle_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class TodoMidRunState:
    """Per-cycle mid-run nudge bookkeeping."""

    attempts: int = 0
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


# --- Durable session state ---


@dataclass(frozen=True, slots=True)
class TodoState:
    """This plugin's session-scope state: plan list, local revision, reminder bookkeeping."""

    revision: int = 0
    phases: tuple[TodoPhase, ...] = ()
    todo_reminder: TodoReminderState = TodoReminderState()
    mid_run: TodoMidRunState = TodoMidRunState()
    invocations: int = 0
    cursor_sequence: int = 0
    cycle_run_id: str | None = None

    @property
    def summary(self) -> TodoSummary:
        return todo_summary(self.phases)

    @classmethod
    def from_payload(cls, raw: Mapping[str, object] | None) -> TodoState:
        if raw is None:
            return cls()
        unknown = sorted(str(key) for key in raw if key not in _STATE_KEYS)
        if unknown:
            raise ValueError(f"todo state field '{unknown[0]}' is not supported")
        missing = sorted(_STATE_KEYS - raw.keys())
        if missing:
            raise ValueError("todo state is missing field(s): " + ", ".join(missing))
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"todo state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        revision = raw["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("todo state revision must be a non-negative integer")
        phases = parse_phases(raw["phases"])
        summary = raw["summary"]
        if not isinstance(summary, Mapping) or any(type(value) is not int for value in summary.values()) or dict(summary) != todo_summary(phases):
            raise ValueError("todo state summary does not match phases")
        reminders = parse_reminders(raw["reminders"])
        return cls(
            revision=revision,
            phases=phases,
            todo_reminder=reminders.todo_reminder,
            mid_run=reminders.mid_run,
            invocations=reminders.invocations,
            cursor_sequence=reminders.cursor_sequence,
            cycle_run_id=reminders.cycle_run_id,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": self.revision,
            "phases": phases_payload(self.phases),
            "summary": dict(self.summary),
            "reminders": {
                "todo": {
                    "attempts": self.todo_reminder.attempts,
                    "awaiting_progress": self.todo_reminder.awaiting_progress,
                    "progress_watermark": self.todo_reminder.progress_watermark,
                    "cycle_run_id": self.todo_reminder.cycle_run_id,
                },
                "todo_mid_run": {
                    "attempts": self.mid_run.attempts,
                    "mutations": self.mid_run.mutations,
                    "mutation_baseline": self.mid_run.mutation_baseline,
                    "cycle_run_id": self.mid_run.cycle_run_id,
                },
                "invocations": self.invocations,
                "cursor_sequence": self.cursor_sequence,
                "cycle_run_id": self.cycle_run_id,
            },
        }


@dataclass(frozen=True, slots=True)
class _Reminders:
    todo_reminder: TodoReminderState
    mid_run: TodoMidRunState
    invocations: int
    cursor_sequence: int
    cycle_run_id: str | None


def _non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"todo reminders field '{field}' must be a non-negative integer")
    return value


def _cycle_run_id(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"todo reminders field '{field}' must be a non-empty string or null")
    return value


def parse_reminders(raw: object) -> _Reminders:
    if not isinstance(raw, Mapping):
        raise ValueError("todo state requires a reminders object")
    unknown = sorted(str(key) for key in raw if key not in _REMINDER_KEYS)
    if unknown:
        raise ValueError(f"todo reminders field '{unknown[0]}' is not supported")
    missing = sorted(_REMINDER_KEYS - raw.keys())
    if missing:
        raise ValueError("todo reminders is missing field(s): " + ", ".join(missing))
    todo_raw = raw["todo"]
    mid_raw = raw["todo_mid_run"]
    if not isinstance(todo_raw, Mapping) or not isinstance(mid_raw, Mapping):
        raise ValueError("todo reminders sub-states must be objects")
    todo_unknown = sorted(str(key) for key in todo_raw if key not in {"attempts", "awaiting_progress", "progress_watermark", "cycle_run_id"})
    if todo_unknown:
        raise ValueError(f"todo reminders.todo field '{todo_unknown[0]}' is not supported")
    mid_unknown = sorted(str(key) for key in mid_raw if key not in {"attempts", "mutations", "mutation_baseline", "cycle_run_id"})
    if mid_unknown:
        raise ValueError(f"todo reminders.todo_mid_run field '{mid_unknown[0]}' is not supported")
    awaiting = todo_raw.get("awaiting_progress")
    if not isinstance(awaiting, bool):
        raise ValueError("todo reminders.todo field 'awaiting_progress' must be a boolean")
    return _Reminders(
        todo_reminder=TodoReminderState(
            attempts=_non_negative_int(todo_raw.get("attempts"), field="todo.attempts"),
            awaiting_progress=awaiting,
            progress_watermark=_non_negative_int(todo_raw.get("progress_watermark"), field="todo.progress_watermark"),
            cycle_run_id=_cycle_run_id(todo_raw.get("cycle_run_id"), field="todo.cycle_run_id"),
        ),
        mid_run=TodoMidRunState(
            attempts=_non_negative_int(mid_raw.get("attempts"), field="todo_mid_run.attempts"),
            mutations=_non_negative_int(mid_raw.get("mutations"), field="todo_mid_run.mutations"),
            mutation_baseline=_non_negative_int(mid_raw.get("mutation_baseline"), field="todo_mid_run.mutation_baseline"),
            cycle_run_id=_cycle_run_id(mid_raw.get("cycle_run_id"), field="todo_mid_run.cycle_run_id"),
        ),
        invocations=_non_negative_int(raw["invocations"], field="invocations"),
        cursor_sequence=_non_negative_int(raw["cursor_sequence"], field="cursor_sequence"),
        cycle_run_id=_cycle_run_id(raw["cycle_run_id"], field="cycle_run_id"),
    )


def phases_payload(phases: Sequence[TodoPhase]) -> list[dict[str, object]]:
    return [{"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]} for phase in phases]


def parse_phases(raw_phases: object) -> tuple[TodoPhase, ...]:
    """Validate phases/tasks and enforce one ``in_progress`` task.

    Snapshot arrays arrive as tuples; tests and fresh payloads use lists.
    """
    if not isinstance(raw_phases, (list, tuple)):
        raise ValueError("todo state requires phases array")
    phases: list[TodoPhase] = []
    phase_names: set[str] = set()
    task_contents: set[str] = set()
    active_count = 0
    for raw_phase in raw_phases:
        if not isinstance(raw_phase, Mapping):
            raise ValueError("todo phase must be an object")
        name = raw_phase.get("name")
        raw_tasks = raw_phase.get("tasks")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("todo phase name must be non-empty")
        name = name.strip()
        if name in phase_names:
            raise ValueError(f'Duplicate todo phase "{name}"')
        if not isinstance(raw_tasks, (list, tuple)):
            raise ValueError(f'todo phase "{name}" requires tasks array')
        phase_names.add(name)
        tasks: list[TodoTask] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, Mapping):
                raise ValueError("todo task must be an object")
            content = raw_task.get("content")
            status = raw_task.get("status")
            blocker = raw_task.get("blocker")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("todo task content must be non-empty")
            if not is_todo_status(status):
                raise ValueError(f"todo task has invalid status: {status!r}")
            content = content.strip()
            if content in task_contents:
                raise ValueError(f'Duplicate todo task "{content}"')
            if status == "blocked":
                if blocker is not None and (not isinstance(blocker, str) or not blocker.strip()):
                    raise ValueError("blocked todo reason must be a non-empty string")
            elif blocker is not None:
                raise ValueError("only blocked todo tasks may include blocker")
            normalized: TodoTask = {"content": content, "status": status}
            if isinstance(blocker, str) and blocker.strip():
                normalized["blocker"] = " ".join(blocker.split())
            tasks.append(normalized)
            task_contents.add(content)
            active_count += status == "in_progress"
        phases.append({"name": name, "tasks": tasks})
    if active_count > 1:
        raise ValueError("todo state permits only one in_progress task")
    return tuple(phases)


def todo_event_payload(*, phases: Sequence[TodoPhase], revision: int) -> dict[str, object]:
    summary = todo_summary(phases)
    return {
        "todo_count": summary["total"],
        "active_count": summary["active"],
        "pending_count": summary["pending"],
        "in_progress_count": summary["in_progress"],
        "completed_count": summary["completed"],
        "abandoned_count": summary["abandoned"],
        "blocked_count": summary["blocked"],
        "revision": revision,
        "phases": phases_payload(phases),
        "summary": dict(summary),
    }


# --- Operation engine ---


@dataclass(frozen=True, slots=True)
class _TodoArgs:
    op: str
    list_: tuple[object, ...] | None = None
    task: str | None = None
    phase: str | None = None
    items: tuple[str, ...] | None = None
    reason: str | None = None


def _optional_str(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"todo {field} must be a string")
    return value


def _item_strings(raw_items: object) -> tuple[str, ...]:
    if not isinstance(raw_items, tuple):
        raise ValueError("todo items must be an array")
    items: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            raise ValueError("todo item must be a string")
        items.append(item)
    return tuple(items)


def _parse_args(arguments: Mapping[str, object]) -> _TodoArgs:
    # Snapshot arguments: arrays arrive as tuples.
    op = arguments.get("op")
    if not isinstance(op, str) or op not in _TODO_OPS:
        raise ValueError(f"todo op must be one of {', '.join(_TODO_OPS)}")
    raw_list = arguments.get("list")
    if raw_list is not None and not isinstance(raw_list, tuple):
        raise ValueError("todo list must be an array")
    raw_items = arguments.get("items")
    return _TodoArgs(
        op=op,
        list_=raw_list,
        task=_optional_str(arguments.get("task"), field="task"),
        phase=_optional_str(arguments.get("phase"), field="phase"),
        items=None if raw_items is None else _item_strings(raw_items),
        reason=_optional_str(arguments.get("reason"), field="reason"),
    )


def _normalize_in_progress(phases: list[TodoPhase]) -> None:
    active: TodoTask | None = None
    for phase in phases:
        for task in phase["tasks"]:
            if task["status"] != "in_progress":
                continue
            if active is None:
                active = task
            else:
                task["status"] = "pending"
    if active is not None:
        return
    for phase in phases:
        for task in phase["tasks"]:
            if task["status"] == "pending":
                task["status"] = "in_progress"
                return


def _task_locations(phases: list[TodoPhase], content: str) -> list[TodoTask]:
    return [task for phase in phases for task in phase["tasks"] if task["content"] == content]


def _phase_by_name(phases: list[TodoPhase], name: str) -> TodoPhase | None:
    return next((phase for phase in phases if phase["name"] == name), None)


def _target_tasks(
    phases: list[TodoPhase],
    *,
    task: str | None,
    phase: str | None,
    required: bool = False,
) -> list[TodoTask]:
    if task is not None and phase is not None:
        raise ValueError("todo operation accepts either task or phase, not both")
    if task is not None:
        matches = _task_locations(phases, task)
        if not matches:
            raise ValueError(f'Task "{task}" not found')
        if len(matches) > 1:
            raise ValueError(f'Task "{task}" is not unique')
        return matches
    if phase is not None:
        target = _phase_by_name(phases, phase)
        if target is None:
            raise ValueError(f'Phase "{phase}" not found')
        return target["tasks"]
    if required:
        raise ValueError("todo operation requires a task or phase target")
    return [item for current in phases for item in current["tasks"]]


def _init_phases(args: _TodoArgs) -> list[TodoPhase]:
    if args.list_ is not None and args.items is not None:
        raise ValueError("init accepts list or items, not both")
    if args.list_ is None and (args.items is None or not args.items):
        raise ValueError("init requires a non-empty list or items")
    if args.list_ is not None:
        phases: list[TodoPhase] = []
        seen_phases: set[str] = set()
        seen_tasks: set[str] = set()
        for raw_phase in args.list_:
            if not isinstance(raw_phase, Mapping):
                raise ValueError("init phase must be an object")
            name = raw_phase.get("phase")
            raw_items = raw_phase.get("items")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("init phase name must be non-empty")
            if name.strip() in seen_phases:
                raise ValueError(f'Duplicate phase "{name.strip()}" in init list')
            if not isinstance(raw_items, (list, tuple)) or not raw_items:
                raise ValueError(f'init phase "{name.strip()}" requires items')
            seen_phases.add(name.strip())
            tasks: list[TodoTask] = []
            for item in raw_items:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError("todo item must be a non-empty string")
                content = item.strip()
                if content in seen_tasks:
                    raise ValueError(f'Duplicate task "{content}" in init list')
                seen_tasks.add(content)
                tasks.append({"content": content, "status": "pending"})
            phases.append({"name": name.strip(), "tasks": tasks})
        return phases
    assert args.items is not None
    phase_name = (args.phase or "Tasks").strip()
    if not phase_name:
        raise ValueError("init phase name must be non-empty")
    tasks: list[TodoTask] = []
    seen_tasks: set[str] = set()
    for item in args.items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("todo item must be a non-empty string")
        content = item.strip()
        if content in seen_tasks:
            raise ValueError(f'Duplicate task "{content}" in init list')
        seen_tasks.add(content)
        tasks.append({"content": content, "status": "pending"})
    return [{"name": phase_name, "tasks": tasks}]


def _apply(args: _TodoArgs, phases: list[TodoPhase]) -> None:
    if args.op == "init":
        phases[:] = _init_phases(args)
        _normalize_in_progress(phases)
        return
    if args.op == "view":
        return
    if args.op == "start":
        task_name = args.task.strip() if args.task is not None else None
        if task_name is None:
            raise ValueError("start requires task")
        target = _target_tasks(phases, task=task_name, phase=None, required=True)[0]
        status = target["status"]
        if status == "blocked":
            raise ValueError(f'Task "{task_name}" is blocked; unblock it before starting')
        if status in _TERMINAL_STATUSES:
            raise ValueError(f'Task "{task_name}" is already {status} and cannot be started')
        for phase in phases:
            for task in phase["tasks"]:
                if task is not target and task["status"] == "in_progress":
                    task["status"] = "pending"
        target["status"] = "in_progress"
        target.pop("blocker", None)
    elif args.op in {"done", "drop"}:
        desired = "completed" if args.op == "done" else "abandoned"
        for target in _target_tasks(phases, task=args.task, phase=args.phase):
            status = target["status"]
            if status == desired:
                continue
            if status in _TERMINAL_STATUSES:
                raise ValueError(f'Task "{target["content"]}" is already {status} and cannot become {desired}')
            target["status"] = desired
            target.pop("blocker", None)
    elif args.op == "block":
        targets = _target_tasks(phases, task=args.task, phase=args.phase, required=True)
        reason = " ".join((args.reason or "").split())
        for target in targets:
            if target["status"] in {"pending", "in_progress", "blocked"}:
                target["status"] = "blocked"
                if reason:
                    target["blocker"] = reason
                else:
                    target.pop("blocker", None)
    elif args.op == "unblock":
        for target in _target_tasks(phases, task=args.task, phase=args.phase, required=True):
            if target["status"] == "blocked":
                target["status"] = "pending"
                target.pop("blocker", None)
    elif args.op == "rm":
        if args.task is not None and args.phase is not None:
            raise ValueError("rm accepts either task or phase, not both")
        if args.task is not None:
            matches = _task_locations(phases, args.task)
            if not matches:
                raise ValueError(f'Task "{args.task}" not found')
            if len(matches) > 1:
                raise ValueError(f'Task "{args.task}" is not unique')
            target = matches[0]
            for phase in phases:
                tasks = phase["tasks"]
                phase["tasks"] = [item for item in tasks if item is not target]
        elif args.phase is not None:
            target = _phase_by_name(phases, args.phase)
            if target is None:
                raise ValueError(f'Phase "{args.phase}" not found')
            target["tasks"] = []
        else:
            for phase in phases:
                phase["tasks"] = []
    elif args.op == "append":
        if args.phase is None or not args.phase.strip():
            raise ValueError("append requires phase")
        if args.items is None or not args.items:
            raise ValueError("append requires non-empty items")
        existing = {task["content"] for phase in phases for task in phase["tasks"]}
        normalized_items: list[str] = []
        for item in args.items:
            content = item.strip()
            if not content:
                raise ValueError("todo item must be a non-empty string")
            if content in existing:
                raise ValueError(f'Task "{content}" already exists')
            existing.add(content)
            normalized_items.append(content)
        target = _phase_by_name(phases, args.phase.strip())
        if target is None:
            new_phase: TodoPhase = {"name": args.phase.strip(), "tasks": []}
            phases.append(new_phase)
            target = new_phase
        target["tasks"].extend({"content": content, "status": "pending"} for content in normalized_items)
    else:
        raise ValueError(f"unsupported todo operation: {args.op}")
    _normalize_in_progress(phases)


def _render(phases: list[TodoPhase], op: str, *, original: list[TodoPhase]) -> str:
    if op in {"view", "init"}:
        if not any(phase["tasks"] for phase in phases):
            return "Todo list is empty."
        return f"Todo {op} applied.\n{_render_table(phases)}"

    lines = [f"Todo {op} applied."]
    old_tasks = {(phase["name"], task["content"]): (phase["name"], task) for phase in original for task in phase["tasks"]}
    new_tasks = {(phase["name"], task["content"]): (phase["name"], task) for phase in phases for task in phase["tasks"]}
    for key in dict.fromkeys((*old_tasks, *new_tasks)):
        old = old_tasks.get(key)
        new = new_tasks.get(key)
        if old == new:
            continue
        if new is None:
            assert old is not None
            lines.append(f"- [removed] {old[1]['content']} ({old[0]})")
        else:
            lines.append(f"- [{new[1]['status']}] {new[1]['content']} ({new[0]})")

    active = next((task["content"] for phase in phases for task in phase["tasks"] if task["status"] == "in_progress"), None)
    summary = todo_summary(phases)
    lines.append(f"Next active: {active or 'none'}")
    lines.append(
        f"Summary: {summary['total']} total; {summary['pending']} pending; {summary['in_progress']} in progress; "
        f"{summary['completed']} completed; {summary['abandoned']} abandoned; {summary['blocked']} blocked."
    )
    return "\n".join(lines)


def _render_table(phases: Sequence[TodoPhase]) -> str:
    """User-facing table without the tool's "applied" header; identical rows otherwise."""
    if not any(phase["tasks"] for phase in phases):
        return "Todo list is empty."
    lines: list[str] = []
    for phase in phases:
        lines.append(f"{phase['name']}:")
        for task in phase["tasks"]:
            blocker = f" — {task['blocker']}" if task.get("blocker") else ""
            lines.append(f"- [{task['status']}] {task['content']}{blocker}")
    return "\n".join(lines)


# --- Provider plan truth ---


def render_provider_state(phases: Sequence[TodoPhase]) -> str | None:
    """Plan-truth prompt text, or None when no active task remains."""
    active_phases: tuple[TodoPhase, ...] = tuple(
        {"name": phase["name"], "tasks": [task for task in phase["tasks"] if task["status"] in _ACTIVE_PROVIDER_STATUSES]} for phase in phases
    )
    active_phases = tuple(phase for phase in active_phases if phase["tasks"])
    if not active_phases:
        return None
    lines = [
        "Runtime-managed todo state is active for this session.",
        "Use this as the current plan truth; do not recreate it from older tool results.",
        "Use one todo operation at a time; view is read-only and mutations are persisted by the runtime.",
        "Current active todos:",
    ]
    for phase in active_phases:
        lines.append(f"{phase['name']}:")
        for task in phase["tasks"]:
            blocker = f" (blocked: {task['blocker']})" if task.get("blocker") else ""
            lines.append(f"- [{task['status']}] {task['content']}{blocker}")
    return "\n".join(lines)


# --- Reminder policy ---

#: Mutation-tool calls since the last todo touch that trigger a mid-run nudge.
TODO_MID_RUN_MUTATION_THRESHOLD = 12
TODO_MID_RUN_MAX_PER_CYCLE = 2
DEFAULT_TODO_REMINDER_MAX_PER_CYCLE = 3
#: Role the reminder must keep: a trailing user message, never a system segment.
TODO_REMINDER_ROLE: Literal["user"] = "user"
TODO_REMINDER_KIND = "todo"
TODO_MID_RUN_KIND = "todo_mid_run"


def decide_todo_mid_run_nudge(
    *,
    max_per_cycle: int = TODO_MID_RUN_MAX_PER_CYCLE,
    state: TodoMidRunState,
    run_id: str | None,
    mutations: int,
    incomplete_count: int,
) -> ReminderDecision[TodoMidRunState]:
    """Whether the next provider call earns a mid-run todo nudge."""
    fresh = TodoMidRunState(cycle_run_id=run_id)
    cycle_state = state if state.cycle_run_id == run_id else fresh
    baseline = min(cycle_state.mutation_baseline, mutations)
    pending = mutations - baseline
    counters = TodoMidRunState(
        attempts=cycle_state.attempts,
        mutations=pending,
        mutation_baseline=baseline,
        cycle_run_id=run_id,
    )
    silent: ReminderDecision[TodoMidRunState] = ReminderDecision(state=counters)
    if incomplete_count == 0:
        return silent
    if pending < TODO_MID_RUN_MUTATION_THRESHOLD or counters.attempts >= max_per_cycle:
        return silent
    attempt = counters.attempts + 1
    return ReminderDecision[TodoMidRunState](
        state=TodoMidRunState(attempts=attempt, mutations=0, mutation_baseline=mutations, cycle_run_id=run_id),
        text=todo_mid_run_nudge_text(
            mutation_count=pending,
            incomplete_count=incomplete_count,
            attempt=attempt,
            max_attempts=max_per_cycle,
        ),
    )


def todo_mid_run_nudge_text(*, mutation_count: int, incomplete_count: int, attempt: int, max_attempts: int) -> str:
    """Bounded mid-run nudge body."""
    return "\n".join(
        [
            '<system-reminder reason="todo_stale">',
            f"{mutation_count} mutating tool call(s) have run since the last todo update, and {incomplete_count} todo item(s) are still open.",
            "Reconcile the list before continuing: mark finished work done and record what is left.",
            f"(Mid-run nudge {attempt}/{max_attempts})",
            "</system-reminder>",
        ]
    )


def incomplete_todo_phases(phases: Sequence[TodoPhase]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Project phases down to the incomplete tasks a reminder lists."""
    incomplete: list[tuple[str, tuple[str, ...]]] = []
    for phase in phases:
        contents = tuple(task["content"] for task in phase["tasks"] if task["status"] in _INCOMPLETE_STATUSES)
        if contents:
            incomplete.append((phase["name"], contents))
    return tuple(incomplete)


def decide_todo_reminder(
    *,
    max_per_cycle: int,
    state: TodoReminderState,
    run_id: str | None,
    incomplete_phases: tuple[tuple[str, tuple[str, ...]], ...],
    tool_result_count: int,
) -> ReminderDecision[TodoReminderState]:
    """Whether a terminal turn earns a todo completion reminder."""
    fresh = TodoReminderState(cycle_run_id=run_id)
    cycle_state = state if state.cycle_run_id == run_id else fresh
    silent: ReminderDecision[TodoReminderState] = ReminderDecision(state=cycle_state)
    if not incomplete_phases:
        return replace(silent, state=fresh)
    if cycle_state.awaiting_progress and tool_result_count <= cycle_state.progress_watermark:
        return silent
    progressed = replace(cycle_state, awaiting_progress=False)
    if progressed.attempts >= max_per_cycle:
        return replace(silent, state=progressed)
    attempt = progressed.attempts + 1
    return ReminderDecision[TodoReminderState](
        state=TodoReminderState(
            attempts=attempt,
            awaiting_progress=True,
            progress_watermark=tool_result_count,
            cycle_run_id=run_id,
        ),
        text=todo_reminder_text(incomplete_phases, attempt=attempt, max_attempts=max_per_cycle),
    )


def todo_reminder_text(
    incomplete_phases: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    attempt: int,
    max_attempts: int,
) -> str:
    """Bounded reminder body sent to the provider."""
    incomplete_count = sum(len(contents) for _name, contents in incomplete_phases)
    lines = ["<system-reminder>", f"You stopped with {incomplete_count} incomplete todo item(s):"]
    for name, contents in incomplete_phases:
        lines.append(f"- {name}")
        lines.extend(f"  - {content}" for content in contents)
    lines.append("")
    lines.append("Please continue working on these tasks or mark them complete if finished.")
    lines.append(f"(Reminder {attempt}/{max_attempts})")
    lines.append("</system-reminder>")
    return "\n".join(lines)


# --- Reminder wiring ---


@dataclass(frozen=True, slots=True)
class TodoRemindersConfig:
    """Reminder policy from the plugin config."""

    enabled: bool = True
    max_per_cycle: int = DEFAULT_TODO_REMINDER_MAX_PER_CYCLE


def parse_todo_config(config: Mapping[str, object] | None) -> TodoRemindersConfig:
    if config is None:
        return TodoRemindersConfig()
    unknown = sorted(str(key) for key in config if key not in {"reminders", "providers"})
    if unknown:
        raise ValueError(f"todo config field '{unknown[0]}' is not supported")
    raw_reminders = config.get("reminders")
    if raw_reminders is None:
        return TodoRemindersConfig()
    if not isinstance(raw_reminders, Mapping):
        raise ValueError("todo config 'reminders' must be an object")
    unknown_reminders = sorted(str(key) for key in raw_reminders if key not in {"enabled", "todo"})
    if unknown_reminders:
        raise ValueError(f"todo config reminders field '{unknown_reminders[0]}' is not supported")
    enabled = raw_reminders.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("todo config reminders.enabled must be a boolean")
    max_per_cycle = DEFAULT_TODO_REMINDER_MAX_PER_CYCLE
    raw_todo = raw_reminders.get("todo")
    if raw_todo is not None:
        if not isinstance(raw_todo, Mapping):
            raise ValueError("todo config reminders.todo must be an object")
        unknown_todo = sorted(str(key) for key in raw_todo if key != "max_per_cycle")
        if unknown_todo:
            raise ValueError(f"todo config reminders.todo field '{unknown_todo[0]}' is not supported")
        candidate = raw_todo.get("max_per_cycle", DEFAULT_TODO_REMINDER_MAX_PER_CYCLE)
        if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 0:
            raise ValueError("todo config reminders.todo.max_per_cycle must be a non-negative integer")
        max_per_cycle = candidate
    return TodoRemindersConfig(enabled=enabled, max_per_cycle=max_per_cycle)


def _sync_cycle(state: TodoState, services: PluginServices, run_id: str) -> tuple[TodoState, str | None]:
    """Only a fresh user request starts a reminder cycle; resumes retain its budget."""
    cursor = state.cursor_sequence
    user_run_id: str | None = None
    for _ in range(_MAX_EVENT_PAGES):
        page = services.read_facts(after_sequence=cursor, limit=_EVENT_PAGE_SIZE)
        for fact in page.facts:
            if isinstance(fact, RunRequestedFact) and not fact.resumed:
                user_run_id = fact.run_id
        cursor = page.next_sequence
        if not page.has_more:
            break
    if user_run_id is not None:
        fresh = replace(
            state,
            cursor_sequence=cursor,
            cycle_run_id=user_run_id,
            todo_reminder=TodoReminderState(cycle_run_id=user_run_id),
            mid_run=TodoMidRunState(cycle_run_id=user_run_id),
            invocations=0,
        )
        return fresh, user_run_id
    if cursor != state.cursor_sequence:
        return replace(state, cursor_sequence=cursor), state.cycle_run_id or run_id
    return state, state.cycle_run_id or run_id


def _incomplete_count(phases: Sequence[TodoPhase]) -> int:
    return sum(1 for phase in phases for task in phase["tasks"] if task["status"] in _INCOMPLETE_STATUSES)


def _account_invocation(state: TodoState, fact: InvocationFinishedFact, cycle: str) -> tuple[TodoState, str | None]:
    mid = state.mid_run if state.mid_run.cycle_run_id == cycle else TodoMidRunState(cycle_run_id=cycle)
    if fact.status == "ok" and fact.tool_name in OWN_TOOL_NAMES and fact.owner == OWNER:
        # A todo touch clears the mutation counter.
        mid = replace(mid, mutations=0, mutation_baseline=0)
    elif fact.status == "ok" and fact.tool_name not in OWN_TOOL_NAMES and not is_read_tier(frozenset(ToolEffect(effect) for effect in fact.effects)):
        mid = replace(mid, mutations=mid.mutations + 1)
    decision = decide_todo_mid_run_nudge(
        state=mid,
        run_id=cycle,
        mutations=mid.mutation_baseline + mid.mutations,
        incomplete_count=_incomplete_count(state.phases),
    )
    updated = replace(state, mid_run=decision.state, cycle_run_id=cycle, invocations=state.invocations + 1)
    return updated, decision.text


def _account_run_finish(state: TodoState, fact: RunFinishedFact, cycle: str, max_per_cycle: int) -> tuple[TodoState, str | None]:
    if fact.status != "completed" or fact.stop_cause not in {"completed", None}:
        return state, None
    reminder = state.todo_reminder if state.todo_reminder.cycle_run_id == cycle else TodoReminderState(cycle_run_id=cycle)
    decision = decide_todo_reminder(
        max_per_cycle=max_per_cycle,
        state=reminder,
        run_id=cycle,
        incomplete_phases=incomplete_todo_phases(state.phases),
        tool_result_count=state.invocations,
    )
    updated = replace(state, todo_reminder=decision.state, cycle_run_id=cycle)
    return updated, decision.text


#: Immutable default so the observer signature carries no call in its defaults.
_DEFAULT_REMINDERS_CONFIG = TodoRemindersConfig()


def todo_observer(
    fact: PluginFact,
    context: ToolContext,
    services: PluginServices,
    *,
    config: TodoRemindersConfig = _DEFAULT_REMINDERS_CONFIG,
) -> None:
    """Account reminders and hand each injection to the core as a user-role continuation."""
    _ = context
    if not config.enabled:
        return
    state_data, version = services.get_state("session")
    original = state = TodoState.from_payload(state_data)
    state, cycle = _sync_cycle(state, services, fact.run_id)
    cycle_id = cycle if cycle is not None else fact.run_id
    reminder_type: str | None = None
    attempt = 0
    max_attempts = 0
    incomplete_count = 0
    mutation_count = 0
    text: str | None = None
    if isinstance(fact, InvocationFinishedFact):
        state, text = _account_invocation(state, fact, cycle_id)
        if text is not None:
            mid_state = state.mid_run
            reminder_type = TODO_MID_RUN_KIND
            attempt = mid_state.attempts
            max_attempts = TODO_MID_RUN_MAX_PER_CYCLE
            incomplete_count = _incomplete_count(state.phases)
            mutation_count = mid_state.mutation_baseline
    elif isinstance(fact, RunFinishedFact):
        state, text = _account_run_finish(state, fact, cycle_id, config.max_per_cycle)
        if text is not None:
            todo_state = state.todo_reminder
            reminder_type = TODO_REMINDER_KIND
            attempt = todo_state.attempts
            max_attempts = config.max_per_cycle
            incomplete_count = _incomplete_count(state.phases)
    else:
        return

    if text is not None and reminder_type is not None:
        payload: dict[str, object] = {
            "kind": reminder_type,
            "text": text,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "incomplete_count": incomplete_count,
        }
        if reminder_type == TODO_MID_RUN_KIND:
            payload["mutation_count"] = mutation_count
        services.enqueue_continuation(
            payload,
            idempotency_key=f"todo:{reminder_type}:{cycle_id}:{attempt}",
            role=TODO_REMINDER_ROLE,
        )
        event: dict[str, object] = {
            "reminder_type": reminder_type,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "incomplete_count": incomplete_count,
        }
        if reminder_type == TODO_MID_RUN_KIND:
            event["mutation_count"] = mutation_count
        services.stage_event("reminder_injected", event)

    if state != original:
        services.replace_state(state.to_payload(), expected_version=version)


# --- Tool ---


@dataclass(frozen=True, slots=True)
class TodoResultBody:
    phases: tuple[TodoPhase, ...]
    summary: TodoSummary
    op: str
    mutated: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", tuple(deepcopy(phase) for phase in self.phases))
        object.__setattr__(self, "summary", deepcopy(self.summary))

    def as_payload(self) -> dict[str, object]:
        return {
            "phases": phases_payload(self.phases),
            "summary": dict(self.summary),
            "op": self.op,
            "mutated": self.mutated,
        }


TODO_DESCRIPTION = (
    "Apply one operation to the runtime-owned todo list. The operation is one of "
    "init, start, done, drop, block, unblock, append, rm, or view. Use init with "
    "list=[{phase, items}] (or items plus optional phase) to create a plan. Use "
    "exact task content and phase names for targeting. Start a task before starting "
    "implementation and mark it done immediately after finishing. Only one task can "
    "be in_progress; the runtime normalizes that invariant. block accepts an optional "
    "reason, and view never mutates state. This tool does not write workspace files."
)


def _load_state(services: PluginServices) -> tuple[TodoState, int]:
    state_data, version = services.get_state("session")
    return TodoState.from_payload(state_data), version


@dataclass(frozen=True, slots=True)
class _TodoChange:
    state: TodoState
    original: list[TodoPhase]
    mutated: bool


def _apply_and_store(args: _TodoArgs, services: PluginServices, *, expected_revision: int | None = None) -> _TodoChange:
    """One mutation path for model tools and interactive user commands."""
    state, version = _load_state(services)
    if expected_revision is not None and state.revision != expected_revision:
        raise ValueError("Todo list changed while this editor was open; reopen /todo before updating")
    phases = [deepcopy(phase) for phase in state.phases]
    original = deepcopy(phases)
    _apply(args, phases)
    mutated = phases != original
    updated = replace(state, revision=state.revision + int(mutated), phases=tuple(phases))
    if mutated:
        services.replace_state(updated.to_payload(), expected_version=version)
        services.stage_event("todo_updated", todo_event_payload(phases=phases, revision=updated.revision))
    return _TodoChange(state=updated, original=original, mutated=mutated)


class TodoTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="todo",
        icon=Icon(glyph="☑"),
        description=TODO_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": list(_TODO_OPS)},
                "list": {
                    "type": "array",
                    "description": "init phases, each with phase and non-empty items",
                    "items": {
                        "type": "object",
                        "properties": {
                            "phase": {"type": "string"},
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["phase", "items"],
                        "additionalProperties": False,
                    },
                },
                "task": {"type": "string", "description": "Exact task content target"},
                "phase": {"type": "string", "description": "Exact phase name target or append destination"},
                "items": {"type": "array", "items": {"type": "string"}, "description": "Task strings for init or append"},
                "reason": {"type": "string", "description": "Optional reason for block"},
            },
            "required": ["op"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        context.require_session_id()
        try:
            args = _parse_args(call.arguments)
            change = _apply_and_store(args, services)
        except ValueError as exc:
            message = str(exc)
            return ToolFailure(tool_name=self.definition.name, error=message, output=TextOutput(text=message))

        phases = list(change.state.phases)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(_render(phases, args.op, original=change.original)),
            body=TodoResultBody(
                phases=tuple(phases),
                summary=todo_summary(phases),
                op=args.op,
                mutated=change.mutated,
            ),
        )


# --- Commands ---


class TodoStatusCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo",
        description="Show the current todo list.",
        argument_schema={
            "type": "object",
            "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        offset, limit = invocation.arguments.get("offset", 0), invocation.arguments.get("limit", 10)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
            return CommandResult(status="error", error="offset must be non-negative and limit must be between 1 and 10")
        state, _ = _load_state(services)
        if offset > state.summary["total"]:
            return CommandResult(status="error", error="Todo offset is beyond the end of the list")
        return CommandResult(
            status="ok",
            output=_render_table(state.phases),
            data={"phases": phases_payload(state.phases), "summary": dict(state.summary), "revision": state.revision},
            presentation=_todo_presentation(state, offset=offset, limit=limit),
        )


def _task_id(task: TodoTask) -> str:
    return hashlib.sha256(task["content"].encode("utf-8")).hexdigest()


def _task_from_id(state: TodoState, identifier: object) -> TodoTask:
    if not isinstance(identifier, str):
        raise ValueError("Choose a todo task")
    task = next((task for phase in state.phases for task in phase["tasks"] if _task_id(task) == identifier), None)
    if task is None:
        raise ValueError("Todo task is no longer available; reopen /todo")
    return task


def _todo_presentation(state: TodoState, *, offset: int = 0, limit: int = 10) -> CommandPresentation:
    tasks = [(phase["name"], task) for phase in state.phases for task in phase["tasks"]]
    count = min(limit, max(0, len(tasks) - offset))
    while True:
        selected = tasks[offset : offset + count]
        lines = [f"Tasks {offset}–{offset + len(selected)} of {len(tasks)}", ""]
        for phase, task in selected:
            blocker = f" — {task['blocker'][:120]}" if task.get("blocker") else ""
            preview = task["content"][:400] + ("…" if len(task["content"]) > 400 else "")
            lines.append(f"- **{phase[:80]}** · [{task['status']}] {preview}{blocker}")
        if not tasks:
            lines.append("Todo list is empty. Add tasks to start planning.")
        actions: list[dict[str, object]] = [{"label": "Add tasks", "action": "todo.add", "prepare_input": True}]
        if offset:
            actions.append({"label": "Previous tasks", "action": "todo", "arguments": {"offset": max(0, offset - (count or limit)), "limit": limit}})
        if offset + count < len(tasks):
            actions.append({"label": "Next tasks", "action": "todo", "arguments": {"offset": offset + count, "limit": limit}})
        actions.extend(
            {"label": f"Edit: {task['content'][:80]}", "action": "todo.edit", "arguments": {"task_id": _task_id(task)}} for _phase, task in selected
        )
        data = {"content": "\n".join(lines), "actions": actions}
        if len(json.dumps(data).encode("utf-8")) <= MAX_VIEW_DATA_BYTES - 4096:
            return CommandPresentation(title="Todo list", view_type="markdown", data=data)
        if count <= 1:
            raise ValueError("Todo detail exceeds the view limit")
        count = max(1, count // 2)


def _task_operations(task: TodoTask) -> list[dict[str, str]]:
    if task["status"] in _TERMINAL_STATUSES:
        return [{"id": "rm", "label": "Remove task"}]
    options = [{"id": "done", "label": "Mark completed"}, {"id": "drop", "label": "Abandon task"}, {"id": "rm", "label": "Remove task"}]
    if task["status"] == "blocked":
        options.insert(0, {"id": "unblock", "label": "Unblock task"})
    else:
        options.insert(0, {"id": "block", "label": "Block task"})
        if task["status"] != "in_progress":
            options.insert(0, {"id": "start", "label": "Start task"})
    return options


class TodoEditCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.edit",
        description="Choose a task operation without changing the todo list.",
        text_argument="task",
        argument_schema={
            "type": "object",
            "properties": {"task": {"type": "string", "minLength": 1}, "task_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "oneOf": [{"required": ["task"]}, {"required": ["task_id"]}],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = invocation
        state, _ = _load_state(services)
        tasks = [task for phase in state.phases for task in phase["tasks"]]
        if not tasks:
            return _todo_presentation(state)
        return CommandPresentation(
            title="Choose todo task",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "task_id",
                        "label": f"Task ({min(len(tasks), 100)} of {len(tasks)})",
                        "options": [{"id": _task_id(task), "label": f"{task['content'][:160]} [{task['status']}]"} for task in tasks[:100]],
                    }
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        try:
            state, _ = _load_state(services)
            if "task_id" in invocation.arguments:
                task = _task_from_id(state, invocation.arguments["task_id"])
            else:
                raw = invocation.arguments.get("task")
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError("todo.edit requires a task name")
                task = _target_tasks(list(state.phases), task=raw.strip(), phase=None, required=True)[0]
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        return CommandResult(
            status="ok",
            output=f"{task['content']} [{task['status']}]",
            presentation=CommandPresentation(
                title=f"Edit task: {task['content'][:120]} [{task['status']}]",
                view_type="form",
                data={
                    "action": "todo.update",
                    "arguments": {"task_id": _task_id(task), "expected_revision": state.revision},
                    "fields": [
                        {"name": "op", "label": "Operation", "options": _task_operations(task)},
                        {"name": "reason", "label": "Block reason (optional)", "field_type": "multiline", "required": False},
                    ],
                },
            ),
        )


class TodoUpdateCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.update",
        slash_visible=False,
        description="Apply a selected todo operation under its displayed list revision.",
        argument_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "op": {"type": "string", "enum": ["start", "done", "drop", "block", "unblock", "rm"]},
                "reason": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["task_id", "op", "expected_revision"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        revision = invocation.arguments.get("expected_revision")
        if type(revision) is not int or revision < 0:
            return CommandResult(status="error", error="todo.update requires the displayed todo revision")
        if invocation.arguments.get("op") not in ("start", "done", "drop", "block", "unblock", "rm"):
            return CommandResult(status="error", error="Choose a valid task operation")
        try:
            state, _ = _load_state(services)
            task = _task_from_id(state, invocation.arguments.get("task_id"))
            args = _parse_args({**invocation.arguments, "task": task["content"]})
            change = _apply_and_store(args, services, expected_revision=revision)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        return CommandResult(
            status="ok",
            output=_render(list(change.state.phases), args.op, original=change.original),
            data={"revision": change.state.revision, "mutated": change.mutated},
            presentation=_todo_presentation(change.state),
        )


class TodoAddCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.add",
        description="Add tasks to a phase, one task per line.",
        argument_schema={
            "type": "object",
            "properties": {
                "phase": {"type": "string", "minLength": 1},
                "items": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["phase", "items", "expected_revision"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = invocation
        state, _ = _load_state(services)
        return CommandPresentation(
            title="Add todo tasks",
            view_type="form",
            data={
                "action": self.definition.name,
                "arguments": {"expected_revision": state.revision},
                "initial_values": {"phase": state.phases[-1]["name"] if state.phases else "Tasks"},
                "fields": [{"name": "phase", "label": "Phase"}, {"name": "items", "label": "Tasks (one per line)", "field_type": "multiline"}],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        phase, items, revision = invocation.arguments.get("phase"), invocation.arguments.get("items"), invocation.arguments.get("expected_revision")
        if (
            not isinstance(phase, str)
            or not phase.strip()
            or not isinstance(items, str)
            or not items.strip()
            or type(revision) is not int
            or revision < 0
        ):
            return CommandResult(status="error", error="Provide a phase, tasks and the displayed todo revision")
        try:
            change = _apply_and_store(
                _TodoArgs(op="append", phase=phase, items=tuple(line for line in items.splitlines() if line.strip())),
                services,
                expected_revision=revision,
            )
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        return CommandResult(
            status="ok",
            output=_render(list(change.state.phases), "append", original=change.original),
            data={"revision": change.state.revision},
            presentation=_todo_presentation(change.state),
        )


class TodoClearCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.clear",
        description="Clear the todo list.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        state, version = _load_state(services)
        if not state.phases:
            return CommandResult(status="ok", output="No todo list to clear.", data={"cleared": False}, presentation=_todo_presentation(state))
        revision = state.revision + 1
        services.replace_state(TodoState(revision=revision).to_payload(), expected_version=version)
        services.stage_event("todo_cleared", todo_event_payload(phases=(), revision=revision))
        return CommandResult(
            status="ok", output="Todo list cleared.", data={"cleared": True}, presentation=_todo_presentation(TodoState(revision=revision))
        )


# --- Context ---


def todo_context(context: ToolContext, services: PluginServices) -> ContextContribution | None:
    """Contribute the plan truth while active tasks remain."""
    _ = context
    state, _ = _load_state(services)
    text = render_provider_state(state.phases)
    return None if text is None else ContextContribution(content=text)


def prepare_todo_deactivation(
    state: Mapping[str, object],
    scope: str,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Reset per-cycle reminder counters; the plan list and cursor survive.

    Pending continuations are cancelled on deactivate, so kept counters would
    describe injections that no longer exist. A fresh user prompt starts a new cycle.
    """
    _ = (scope, scope_key, tip_sequence)
    parsed = TodoState.from_payload(state)
    return replace(
        parsed,
        todo_reminder=TodoReminderState(),
        mid_run=TodoMidRunState(),
        invocations=0,
        cycle_run_id=None,
    ).to_payload()
