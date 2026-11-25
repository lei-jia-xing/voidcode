"""Todo session state, strict parsing, and event payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from voidcode.capabilities.plugin_state import PluginStateStore
from voidcode.sdk.services import StateServices

from . import limits, text
from .identity import valid_task_id
from .models import (
    TODO_MID_RUN_KIND,
    TODO_REMINDER_KIND,
    ReminderKind,
    ReminderPolicy,
    TodoPhase,
    TodoSummary,
    TodoTask,
    is_todo_status,
    todo_summary,
)

SCHEMA_VERSION = 3
_REMINDER_KEYS: frozenset[str] = frozenset({"todo", "todo_mid_run", "invocations", "cursor_sequence", "cycle_run_id"})


@dataclass(frozen=True, slots=True)
class TodoState:
    """This plugin's session-scope state: plan list, local revision, reminder bookkeeping."""

    revision: int = 0
    phases: tuple[TodoPhase, ...] = ()
    todo_reminder: ReminderPolicy = ReminderPolicy(kind="todo")
    mid_run: ReminderPolicy = ReminderPolicy(kind="todo_mid_run")
    invocations: int = 0
    cursor_sequence: int = 0
    cycle_run_id: str | None = None

    @property
    def summary(self) -> TodoSummary:
        return todo_summary(self.phases)

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> TodoState:
        required = ("schema_version", "revision", "phases", "reminders")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"todo state is missing field '{missing[0]}'")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"todo state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        revision = raw["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("todo state revision must be a non-negative integer")
        phases = parse_phases(raw["phases"])
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
            "reminders": {
                "todo": _policy_payload(self.todo_reminder),
                "todo_mid_run": _policy_payload(self.mid_run),
                "invocations": self.invocations,
                "cursor_sequence": self.cursor_sequence,
                "cycle_run_id": self.cycle_run_id,
            },
        }


@dataclass(frozen=True, slots=True)
class _Reminders:
    todo_reminder: ReminderPolicy
    mid_run: ReminderPolicy
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


_POLICY_KEYS: frozenset[str] = frozenset({"attempts", "awaiting_progress", "progress_watermark", "mutations", "mutation_baseline", "cycle_run_id"})


def _parse_policy(raw: object, *, section: str, kind: ReminderKind) -> ReminderPolicy:
    if not isinstance(raw, Mapping):
        raise ValueError(f"todo reminders.{section} must be an object")
    unknown = sorted(str(key) for key in raw if key not in _POLICY_KEYS)
    if unknown:
        raise ValueError(f"todo reminders.{section} field '{unknown[0]}' is not supported")
    awaiting = raw.get("awaiting_progress")
    if not isinstance(awaiting, bool):
        raise ValueError(f"todo reminders.{section} field 'awaiting_progress' must be a boolean")
    return ReminderPolicy(
        kind=kind,
        attempts=_non_negative_int(raw.get("attempts"), field=f"{section}.attempts"),
        awaiting_progress=awaiting,
        progress_watermark=_non_negative_int(raw.get("progress_watermark"), field=f"{section}.progress_watermark"),
        mutations=_non_negative_int(raw.get("mutations"), field=f"{section}.mutations"),
        mutation_baseline=_non_negative_int(raw.get("mutation_baseline"), field=f"{section}.mutation_baseline"),
        cycle_run_id=_cycle_run_id(raw.get("cycle_run_id"), field=f"{section}.cycle_run_id"),
    )


def _policy_payload(policy: ReminderPolicy) -> dict[str, object]:
    return {
        "attempts": policy.attempts,
        "awaiting_progress": policy.awaiting_progress,
        "progress_watermark": policy.progress_watermark,
        "mutations": policy.mutations,
        "mutation_baseline": policy.mutation_baseline,
        "cycle_run_id": policy.cycle_run_id,
    }


def parse_reminders(raw: object) -> _Reminders:
    if not isinstance(raw, Mapping):
        raise ValueError("todo state requires a reminders object")
    unknown = sorted(str(key) for key in raw if key not in _REMINDER_KEYS)
    if unknown:
        raise ValueError(f"todo reminders field '{unknown[0]}' is not supported")
    missing = sorted(_REMINDER_KEYS - raw.keys())
    if missing:
        raise ValueError("todo reminders is missing field(s): " + ", ".join(missing))
    return _Reminders(
        todo_reminder=_parse_policy(raw["todo"], section="todo", kind=TODO_REMINDER_KIND),
        mid_run=_parse_policy(raw["todo_mid_run"], section="todo_mid_run", kind=TODO_MID_RUN_KIND),
        invocations=_non_negative_int(raw["invocations"], field="invocations"),
        cursor_sequence=_non_negative_int(raw["cursor_sequence"], field="cursor_sequence"),
        cycle_run_id=_cycle_run_id(raw["cycle_run_id"], field="cycle_run_id"),
    )


def phases_payload(phases: Sequence[TodoPhase]) -> list[dict[str, object]]:
    return [{"name": phase["name"], "tasks": [dict(task) for task in phase["tasks"]]} for phase in phases]


def parse_phases(raw_phases: object) -> tuple[TodoPhase, ...]:
    """Validate phases/tasks and enforce one ``in_progress`` task.

    Snapshot arrays arrive as tuples; tests and fresh payloads use lists.
    Task ids are stored identities, not content hashes, so repeated wording is
    allowed without letting an old UI action target a different task.
    """
    if not isinstance(raw_phases, (list, tuple)):
        raise ValueError("todo state requires phases array")
    if len(raw_phases) > limits.MAX_TODO_PHASES:
        raise ValueError(f"todo state permits at most {limits.MAX_TODO_PHASES} phases")
    phases: list[TodoPhase] = []
    phase_names: set[str] = set()
    task_ids: set[str] = set()
    active_count = 0
    total_tasks = 0
    for raw_phase in raw_phases:
        if not isinstance(raw_phase, Mapping):
            raise ValueError("todo phase must be an object")
        unknown_phase = sorted(str(key) for key in raw_phase if key not in {"name", "tasks"})
        if unknown_phase:
            raise ValueError(f"todo phase field '{unknown_phase[0]}' is not supported")
        name = raw_phase.get("name")
        raw_tasks = raw_phase.get("tasks")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("todo phase name must be non-empty")
        name = name.strip()
        text.require_text_bytes(name, field="phase name", limit=limits.MAX_TODO_PHASE_NAME_BYTES)
        if name in phase_names:
            raise ValueError(f'Duplicate todo phase "{name}"')
        if not isinstance(raw_tasks, (list, tuple)):
            raise ValueError(f'todo phase "{name}" requires tasks array')
        if len(raw_tasks) > limits.MAX_TODO_TASKS_PER_PHASE:
            raise ValueError(f'todo phase "{name}" permits at most {limits.MAX_TODO_TASKS_PER_PHASE} tasks')
        phase_names.add(name)
        tasks: list[TodoTask] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, Mapping):
                raise ValueError("todo task must be an object")
            unknown_task = sorted(str(key) for key in raw_task if key not in {"id", "content", "status", "blocker"})
            if unknown_task:
                raise ValueError(f"todo task field '{unknown_task[0]}' is not supported")
            task_id = raw_task.get("id")
            content = raw_task.get("content")
            status = raw_task.get("status")
            blocker = raw_task.get("blocker")
            if not valid_task_id(task_id):
                raise ValueError(f"todo task id must be a 32-character lowercase hex string: {task_id!r}")
            if task_id in task_ids:
                raise ValueError(f'Duplicate todo task id "{task_id}"')
            if not isinstance(content, str) or not content.strip():
                raise ValueError("todo task content must be non-empty")
            if not is_todo_status(status):
                raise ValueError(f"todo task has invalid status: {status!r}")
            content = content.strip()
            text.require_text_bytes(content, field="task content", limit=limits.MAX_TODO_TASK_CONTENT_BYTES)
            if status == "blocked":
                if blocker is not None and (not isinstance(blocker, str) or not blocker.strip()):
                    raise ValueError("blocked todo reason must be a non-empty string")
            elif blocker is not None:
                raise ValueError("only blocked todo tasks may include blocker")
            normalized: TodoTask = {"id": task_id, "content": content, "status": status}
            if isinstance(blocker, str) and blocker.strip():
                reason = " ".join(blocker.split())
                text.require_text_bytes(reason, field="task blocker", limit=limits.MAX_TODO_BLOCKER_BYTES)
                normalized["blocker"] = reason
            tasks.append(normalized)
            task_ids.add(task_id)
            active_count += status == "in_progress"
        total_tasks += len(tasks)
        if total_tasks > limits.MAX_TODO_TASKS:
            raise ValueError(f"todo state permits at most {limits.MAX_TODO_TASKS} tasks")
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


def _store(services: StateServices) -> PluginStateStore[TodoState]:
    """This plugin's session-state store: strict keys, typed parse, CAS save."""
    return PluginStateStore(
        services,
        keys=frozenset({"schema_version", "revision", "phases", "reminders"}),
        parse=TodoState.from_payload,
        serialize=TodoState.to_payload,
        empty=TodoState,
        unknown=lambda extra: f"todo state field '{extra[0]}' is not supported",
        missing=lambda absent: "todo state is missing field(s): " + ", ".join(absent),
    )
