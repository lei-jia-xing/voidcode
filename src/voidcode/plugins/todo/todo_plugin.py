"""Todo plugin: runtime-owned plan list, plan truth context, and reminder policy.

Ported from the legacy runtime mechanism without behavior changes:

- Op engine, rendering and argument errors: ``legacy tools/todo.py`` (pure compute, whole-op
  atomicity, one ``in_progress`` invariant, first-pending promotion, blocked never promoted).
- State validation shape: ``legacy runtime/todos.py`` (strict unknown-key rejection, summary
  must equal the projection of phases, blocker only while blocked).
- Plan-truth provider text: ``legacy runtime/todos.py:render_provider_todo_state`` verbatim.
- Reminder decisions and texts: ``legacy runtime/reminders.py`` verbatim.

Persistence moves from the legacy session-metadata back door to the plugin's own session-scope
state document with CAS versioning, and events become plugin-namespaced facts.

Reminders are wired to the core's typed-message seam: both kinds are durable *user-role*
continuations carrying the verbatim legacy text under ``payload["text"]``, so a reminder is never
restated as a system message. The mid-run nudge fires on the invocation that crosses the mutation
threshold and the completion reminder fires when a run finishes with open todos; per-cycle
budgets, watermarks and the cycle identity (the newest genuine user prompt, read from the
session's own committed events) reproduce the legacy accounting. The legacy suppression
inputs ("delegated child", "runtime waits for user", "pending background task", "todo tool
advertised") are not exposed to a plugin, so reminders never suppress on them: a parked run
emits no run-finished fact, so "waits for user" is silent by construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import ClassVar, Literal, TypedDict

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.hooks import ContextContribution, InvocationFinishedFact, PluginFact, RunFinishedFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin, StateSchema
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


# --- Reminder bookkeeping (ported state models) ---


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
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"todo state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        revision = raw["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("todo state revision must be a non-negative integer")
        phases = parse_phases(raw["phases"])
        summary = raw["summary"]
        if not isinstance(summary, Mapping) or dict(summary) != dict(todo_summary(phases)):
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


def _optional_str(value: object, *, field: str) -> str | None:
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
            cycle_run_id=_optional_str(todo_raw.get("cycle_run_id"), field="todo.cycle_run_id"),
        ),
        mid_run=TodoMidRunState(
            attempts=_non_negative_int(mid_raw.get("attempts"), field="todo_mid_run.attempts"),
            mutations=_non_negative_int(mid_raw.get("mutations"), field="todo_mid_run.mutations"),
            mutation_baseline=_non_negative_int(mid_raw.get("mutation_baseline"), field="todo_mid_run.mutation_baseline"),
            cycle_run_id=_optional_str(mid_raw.get("cycle_run_id"), field="todo_mid_run.cycle_run_id"),
        ),
        invocations=_non_negative_int(raw["invocations"], field="invocations"),
        cursor_sequence=_non_negative_int(raw["cursor_sequence"], field="cursor_sequence"),
        cycle_run_id=_optional_str(raw["cycle_run_id"], field="cycle_run_id"),
    )


def migrate_todo_state(payload: Mapping[str, object], from_version: int, to_version: int) -> Mapping[str, object]:
    """Pure v1 -> v2 migration: add empty reminder bookkeeping, rewrite nothing else."""
    if from_version != 1 or to_version != SCHEMA_VERSION:
        raise ValueError(f"todo state migration supports 1 -> {SCHEMA_VERSION}, got {from_version} -> {to_version}")
    migrated = dict(payload)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated["reminders"] = {
        "todo": {"attempts": 0, "awaiting_progress": False, "progress_watermark": 0, "cycle_run_id": None},
        "todo_mid_run": {"attempts": 0, "mutations": 0, "mutation_baseline": 0, "cycle_run_id": None},
        "invocations": 0,
        "cursor_sequence": 0,
        "cycle_run_id": None,
    }
    return migrated


def phases_payload(phases: Sequence[TodoPhase]) -> list[dict[str, object]]:
    return [{"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]} for phase in phases]


def parse_phases(raw_phases: object) -> tuple[TodoPhase, ...]:
    """Strict phase/task parsing, including the single ``in_progress`` invariant.

    Arrays arrive either as plain lists (tests, freshly built payloads) or as tuples: plugin
    state is handed to a handler as an immutable JSON snapshot, where every array is a tuple.
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


# --- Operation engine (ported verbatim from legacy tools/todo.py) ---


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
    # Arguments arrive as an immutable JSON snapshot: arrays are tuples here.
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
        lines = [f"Todo {op} applied."]
        for phase in phases:
            lines.append(f"{phase['name']}:")
            for task in phase["tasks"]:
                blocker = f" — {task['blocker']}" if task.get("blocker") else ""
                lines.append(f"- [{task['status']}] {task['content']}{blocker}")
        return "\n".join(lines)

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


# --- Provider plan truth (verbatim legacy text) ---


def render_provider_state(phases: Sequence[TodoPhase]) -> str | None:
    """The legacy plan-truth prompt text, or None when no active task remains."""
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


# --- Reminder policy (ported verbatim; wired through typed user-role continuations) ---

#: Mutation-tool calls since the last todo touch that trigger a mid-run nudge.
TODO_MID_RUN_MUTATION_THRESHOLD = 12
#: Mid-run nudges per cycle.
TODO_MID_RUN_MAX_PER_CYCLE = 2
#: Default per-cycle budget for todo completion reminders.
DEFAULT_TODO_REMINDER_MAX_PER_CYCLE = 3
#: Role the reminder must keep: a trailing user message, never a system segment.
TODO_REMINDER_ROLE: Literal["user"] = "user"
#: Segment metadata source for the per-call reminder channel.
TODO_REMINDER_SOURCE = "runtime_reminder"
#: Reminder kind names.
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


def todo_mutation_count(facts: Sequence[InvocationFinishedFact], *, own_tool_names: frozenset[str]) -> int:
    """Successful mutation invocations since the last successful todo call.

    The declared effects the core reports with each finished invocation decide read tier,
    exactly like the legacy ``is_read_tier`` filter, so a new tool cannot be mis-tiered by a
    handwritten name list.
    """
    last_todo_touch = -1
    for index, fact in enumerate(facts):
        if fact.tool_name in own_tool_names and fact.status == "ok":
            last_todo_touch = index
    return sum(
        1
        for fact in facts[last_todo_touch + 1 :]
        if fact.status == "ok" and fact.tool_name not in own_tool_names and not is_read_tier(_effect_set(fact.effects))
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


# --- Reminder wiring (typed user-role continuations over the core's durable seams) ---


@dataclass(frozen=True, slots=True)
class TodoRemindersConfig:
    """Reminder policy from the plugin config; legacy defaults are enabled + 3 per cycle."""

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
    """Consume newly committed events; a genuine user prompt starts a fresh reminder cycle.

    The legacy cycle identity was the session's current user-prompt run. The new core gives a
    continuation its own run, so the cycle is derived from the session's own facts: the newest
    ``kernel.user_message`` run id, read incrementally from the stored cursor.
    """
    cursor = state.cursor_sequence
    user_run_id: str | None = None
    for _ in range(_MAX_EVENT_PAGES):
        events = services.read_events(after_sequence=cursor, limit=_EVENT_PAGE_SIZE, include_payload=False)
        if not events:
            break
        for event in events:
            if event.event_type == "kernel.user_message":
                user_run_id = event.run_id
        cursor = max(cursor, max(event.sequence for event in events))
        if len(events) < _EVENT_PAGE_SIZE:
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


def _effect_set(effects: Sequence[str]) -> frozenset[ToolEffect]:
    """Map declared effect names to the SDK enum; an unknown name can never be read-tier."""
    mapped: set[ToolEffect] = set()
    for effect in effects:
        try:
            mapped.add(ToolEffect(effect))
        except ValueError:
            continue
    return frozenset(mapped)


def _incomplete_count(phases: Sequence[TodoPhase]) -> int:
    return sum(1 for phase in phases for task in phase["tasks"] if task["status"] in _INCOMPLETE_STATUSES)


def _account_invocation(state: TodoState, fact: InvocationFinishedFact, cycle: str) -> tuple[TodoState, str | None]:
    mid = state.mid_run if state.mid_run.cycle_run_id == cycle else TodoMidRunState(cycle_run_id=cycle)
    if fact.status == "ok" and fact.tool_name in OWN_TOOL_NAMES and fact.owner == OWNER:
        # A todo touch clears the mutation counter, exactly like the legacy recalculation.
        mid = replace(mid, mutations=0, mutation_baseline=0)
    elif fact.status == "ok" and fact.tool_name not in OWN_TOOL_NAMES and not is_read_tier(_effect_set(fact.effects)):
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
    reminder = state.todo_reminder if state.todo_reminder.cycle_run_id == cycle else TodoReminderState(cycle_run_id=cycle)
    decision = decide_todo_reminder(
        max_per_cycle=max_per_cycle,
        state=reminder,
        run_id=cycle,
        incomplete_phases=incomplete_todo_phases(state.phases),
        tool_result_count=state.invocations,
    )
    updated = replace(state, todo_reminder=decision.state, cycle_run_id=cycle)
    if fact.status != "completed":
        # Only a cleanly completed turn earns a reminder; parked/failed/interrupted turns do not.
        return updated, None
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
    state = TodoState.from_payload(state_data)
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

    if state != TodoState.from_payload(state_data):
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


class TodoTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="todo",
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
            state, version = _load_state(services)
            phases: list[TodoPhase] = [deepcopy(phase) for phase in state.phases]
            original = deepcopy(phases)
            _apply(args, phases)
        except ValueError as exc:
            message = str(exc)
            return ToolFailure(tool_name=self.definition.name, error=message, output=TextOutput(text=message))

        mutated = phases != original
        revision = state.revision
        if mutated:
            revision += 1
            services.replace_state(
                TodoState(revision=revision, phases=tuple(phases)).to_payload(),
                expected_version=version,
            )
            services.stage_event("todo_updated", todo_event_payload(phases=phases, revision=revision))

        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(_render(phases, args.op, original=original)),
            body=TodoResultBody(
                phases=tuple(phases),
                summary=todo_summary(phases),
                op=args.op,
                mutated=mutated,
            ),
        )


# --- Commands ---


class TodoStatusCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo",
        description="Show the current todo list.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        state, _ = _load_state(services)
        return CommandResult(
            status="ok",
            output=_render_table(state.phases),
            data={"phases": phases_payload(state.phases), "summary": dict(state.summary), "revision": state.revision},
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
            return CommandResult(status="ok", output="No todo list to clear.", data={"cleared": False})
        revision = state.revision + 1
        services.replace_state(TodoState(revision=revision).to_payload(), expected_version=version)
        services.stage_event("todo_cleared", todo_event_payload(phases=(), revision=revision))
        return CommandResult(status="ok", output="Todo list cleared.", data={"cleared": True})


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
    """Pure deactivation transform: the plan list survives; per-cycle reminder counters reset.

    The activation transaction cancels this owner's pending reminder continuations, so kept
    attempt counters or awaiting flags would describe injections that no longer exist. The
    cursor stays forward-only and the next genuine user prompt starts a fresh cycle anyway.
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


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    reminders = parse_todo_config(config)

    def observer(fact: PluginFact, context: ToolContext, services: PluginServices) -> None:
        todo_observer(fact, context, services, config=reminders)

    return Plugin(
        tools=(TodoTool(),),
        commands=(TodoStatusCommand(), TodoClearCommand()),
        context=todo_context,
        observer=observer,
        observed_facts=("invocation_finished", "run_finished"),
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "revision", "phases", "summary", "reminders"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "revision": {"type": "integer", "minimum": 0},
                    "phases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "tasks"],
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "tasks": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["content", "status"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "content": {"type": "string"},
                                            "status": {"type": "string", "enum": list(TODO_STATUSES)},
                                            "blocker": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "summary": {
                        "type": "object",
                        "required": ["total", "pending", "in_progress", "completed", "abandoned", "blocked", "active"],
                        "additionalProperties": False,
                        "properties": {
                            "total": {"type": "integer"},
                            "pending": {"type": "integer"},
                            "in_progress": {"type": "integer"},
                            "completed": {"type": "integer"},
                            "abandoned": {"type": "integer"},
                            "blocked": {"type": "integer"},
                            "active": {"type": "integer"},
                        },
                    },
                    "reminders": {
                        "type": "object",
                        "required": ["todo", "todo_mid_run", "invocations", "cursor_sequence", "cycle_run_id"],
                        "additionalProperties": False,
                        "properties": {
                            "todo": {
                                "type": "object",
                                "required": ["attempts", "awaiting_progress", "progress_watermark", "cycle_run_id"],
                                "additionalProperties": False,
                                "properties": {
                                    "attempts": {"type": "integer", "minimum": 0},
                                    "awaiting_progress": {"type": "boolean"},
                                    "progress_watermark": {"type": "integer", "minimum": 0},
                                    "cycle_run_id": {"type": ["string", "null"]},
                                },
                            },
                            "todo_mid_run": {
                                "type": "object",
                                "required": ["attempts", "mutations", "mutation_baseline", "cycle_run_id"],
                                "additionalProperties": False,
                                "properties": {
                                    "attempts": {"type": "integer", "minimum": 0},
                                    "mutations": {"type": "integer", "minimum": 0},
                                    "mutation_baseline": {"type": "integer", "minimum": 0},
                                    "cycle_run_id": {"type": ["string", "null"]},
                                },
                            },
                            "invocations": {"type": "integer", "minimum": 0},
                            "cursor_sequence": {"type": "integer", "minimum": 0},
                            "cycle_run_id": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        ),
        migrate_state=migrate_todo_state,
        prepare_deactivate=prepare_todo_deactivation,
    )
