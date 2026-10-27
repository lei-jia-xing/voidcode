"""Cron plugin: scheduled prompts as workspace-product policy over core wakeups.

Ported from the legacy scheduler without behavior changes, but rebased on the core's durable
wakeup seam instead of a runtime scheduler thread plus a workspace ``scheduled_tasks`` table:

- Grammar, next-fire search and human formatting: ``schedule_expr`` (verbatim legacy copy).
- ``/loop`` parsing and confirmation text: ``loop_command`` (verbatim legacy copy).
- Terminal reschedule rules: ``legacy runtime/background/scheduler.py:on_task_terminal`` —
  one-shots delete on success and stay disabled for inspection on failure, recurring schedules
  delete past expiry and otherwise recompute ``next_fire_after(expr, now)``.
- Tool surface, argument errors, 50-job cap and output shapes: ``legacy tools/cron.py``.

Scope moves from a workspace table to this plugin's session-scope state document: the session
that created a schedule owns it, so the same workspace can hold independent schedules per
session, and every schedule keeps exactly ONE armed wakeup — a claimed wakeup means a run is in
flight, which is what makes overlap impossible without a lock. The plugin never sleeps or polls:
the core ticker owns time, and the plugin only computes fire instants and re-arms when it
observes the completion of a run whose wakeup it armed.

Re-arming is the whole point of the terminal reschedule: a long run delays the next fire, and
slots missed while a run was in flight are never replayed.

Divergences from the legacy product, stated rather than faked:

- ``cron_list`` reports each enabled schedule's next fire as recomputed from its expression at
  list time; the armed wakeup remains the truth (a plugin cannot read core wakeup rows). The
  two agree exactly for ``at`` and ``once`` schedules and are within one tick for the rest.
- The legacy "5s scheduler tick" and the expired-sweep pass are gone: expiry is enforced by the
  same wakeup claim (``expires_at`` is checked when a run finishes), so an expired schedule can
  never fire a second time after its final due run.
- There is no configuration surface: the schedule policy (7-day recurring expiry, 50-job cap) is
  the ported legacy policy, so ``create_plugin`` accepts and ignores its config mapping.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import ClassVar, Final, Literal, cast
from uuid import uuid4

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.hooks import PluginFact, RunFinishedFact, WakeupFiredFact
from voidcode.sdk.icons import Icon
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
)

from .loop_command import USAGE_MESSAGE, loop_confirmation_message, parse_loop_command
from .schedule_expr import ScheduleExpr, human_schedule, next_fire_after, parse_schedule

SCHEMA_VERSION: Final[int] = 1
#: Wakeup payload kind this plugin owns; a fired wakeup of any other kind is not ours.
WAKEUP_KIND: Final[str] = "cron"
#: Claude parity: maximum schedules one session may hold (legacy per-workspace cap).
MAX_SCHEDULES_PER_WORKSPACE: Final[int] = 50
#: Claude parity: 7d default expiration for recurring schedules.
DEFAULT_RECURRING_EXPIRY_SECONDS: Final[int] = 7 * 86400

#: One-shot kinds: they fire once and never re-arm.
_ONE_SHOT_KINDS: frozenset[str] = frozenset({"at", "once"})
#: Recurring kinds: they re-arm until their expiry passes.
_RECURRING_KINDS: frozenset[str] = frozenset({"every", "cron"})
_KINDS: frozenset[str] = _ONE_SHOT_KINDS | _RECURRING_KINDS

_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "schedules", "cursor_sequence"})
_SCHEDULE_KEYS: frozenset[str] = frozenset({"schedule_text", "kind", "prompt", "enabled", "expires_at", "wakeup_id"})
#: How many event pages one observation may consume while looking for unconsumed wakeup firings.
_MAX_EVENT_PAGES: Final[int] = 3
_EVENT_PAGE_SIZE: Final[int] = 200

ScheduleKind = Literal["every", "at", "cron", "once"]


def _clock_ms() -> int:
    """The wall clock the schedule math runs on, in the core's due-time unit.

    The legacy scheduler took this as an injectable ``clock``; it stays a module-level seam so a
    test can pin the instant a fire is computed for.
    """
    return int(time.time() * 1000)


# --- Durable session state ---


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One schedule: its expression, its prompt, and the wakeup currently armed for it."""

    schedule_text: str
    kind: ScheduleKind
    prompt: str
    enabled: bool
    expires_at: int | None
    wakeup_id: str | None

    @classmethod
    def from_payload(cls, raw: Mapping[str, object], *, schedule_id: str) -> ScheduleEntry:
        unknown = sorted(str(key) for key in raw if key not in _SCHEDULE_KEYS)
        if unknown:
            raise ValueError(f"cron schedule '{schedule_id}' field '{unknown[0]}' is not supported")
        missing = sorted(_SCHEDULE_KEYS - raw.keys())
        if missing:
            raise ValueError(f"cron schedule '{schedule_id}' is missing field(s): " + ", ".join(missing))
        raw_kind = raw["kind"]
        if raw_kind not in _KINDS:
            raise ValueError(f"cron schedule '{schedule_id}' kind must be one of {sorted(_KINDS)}, got {raw_kind!r}")
        kind = cast(ScheduleKind, raw_kind)
        enabled = raw["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(f"cron schedule '{schedule_id}' enabled must be a boolean")
        return cls(
            schedule_text=_required_text(raw["schedule_text"], field=f"cron schedule '{schedule_id}' schedule_text"),
            kind=kind,
            prompt=_required_text(raw["prompt"], field=f"cron schedule '{schedule_id}' prompt"),
            enabled=enabled,
            expires_at=_optional_ms(raw["expires_at"], field=f"cron schedule '{schedule_id}' expires_at"),
            wakeup_id=_optional_text(raw["wakeup_id"], field=f"cron schedule '{schedule_id}' wakeup_id"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schedule_text": self.schedule_text,
            "kind": self.kind,
            "prompt": self.prompt,
            "enabled": self.enabled,
            "expires_at": self.expires_at,
            "wakeup_id": self.wakeup_id,
        }


@dataclass(frozen=True, slots=True)
class CronState:
    """This plugin's session-scope state: the session's schedules and its event cursor."""

    schedules: Mapping[str, ScheduleEntry] = field(default_factory=dict)
    cursor_sequence: int = 0

    @classmethod
    def from_payload(cls, raw: Mapping[str, object] | None) -> CronState:
        if raw is None:
            return cls()
        unknown = sorted(str(key) for key in raw if key not in _STATE_KEYS)
        if unknown:
            raise ValueError(f"cron state field '{unknown[0]}' is not supported")
        missing = sorted(_STATE_KEYS - raw.keys())
        if missing:
            raise ValueError("cron state is missing field(s): " + ", ".join(missing))
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"cron state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        cursor = raw["cursor_sequence"]
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("cron state cursor_sequence must be a non-negative integer")
        raw_schedules = raw["schedules"]
        if not isinstance(raw_schedules, Mapping):
            raise ValueError("cron state schedules must be an object")
        schedules: dict[str, ScheduleEntry] = {}
        for schedule_id, entry in raw_schedules.items():
            if not isinstance(schedule_id, str) or not schedule_id:
                raise ValueError("cron state schedule ids must be non-empty strings")
            if not isinstance(entry, Mapping):
                raise ValueError(f"cron schedule '{schedule_id}' must be an object")
            schedules[schedule_id] = ScheduleEntry.from_payload(entry, schedule_id=schedule_id)
        return cls(schedules=schedules, cursor_sequence=cursor)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "schedules": {schedule_id: entry.to_payload() for schedule_id, entry in self.schedules.items()},
            "cursor_sequence": self.cursor_sequence,
        }


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field)


def _required_ms(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _optional_ms(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _required_ms(value, field=field)


# --- Wakeup arming (one armed wakeup per schedule, keyed by its fire instant) ---


def _arm(services: PluginServices, *, schedule_id: str, prompt: str, due_at_ms: int) -> str:
    """Schedule the next fire of one schedule as a user-role wakeup of the owning session.

    The idempotency key is the schedule plus the exact fire instant, so re-arming the same future
    point is deterministic and can never duplicate a wakeup.
    """
    return services.schedule_wakeup(
        {"kind": WAKEUP_KIND, "text": prompt, "schedule_id": schedule_id},
        due_at_ms=due_at_ms,
        idempotency_key=f"cron:{schedule_id}:{due_at_ms}",
        role="user",
    )


def _disarm(services: PluginServices, wakeup_id: str | None) -> None:
    """Cancel a pending wakeup; a claimed one is already spent and stays untouched."""
    if wakeup_id:
        services.cancel_wakeup(wakeup_id)


def _load_state(services: PluginServices) -> tuple[CronState, int]:
    state_data, version = services.get_state("session")
    return CronState.from_payload(state_data), version


def _write_state(services: PluginServices, state: CronState, *, expected_version: int) -> None:
    services.replace_state(state.to_payload(), expected_version=expected_version)


def _expiry_for(expr: ScheduleExpr, *, now_ms: int) -> int | None:
    """Recurring schedules expire after 7 days; one-shots never expire (legacy defaults)."""
    if expr.kind in _RECURRING_KINDS:
        return now_ms + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000
    return None


def _created_event(*, schedule_id: str, entry: ScheduleEntry, expr: ScheduleExpr, next_fire_ms: int) -> dict[str, object]:
    return {
        "id": schedule_id,
        "schedule_text": entry.schedule_text,
        "kind": entry.kind,
        "human_schedule": human_schedule(expr),
        "prompt": entry.prompt,
        "next_fire_at": next_fire_ms,
        "expires_at": entry.expires_at,
        "wakeup_id": entry.wakeup_id,
    }


def _create(
    services: PluginServices,
    *,
    state: CronState,
    version: int,
    expr: ScheduleExpr,
    schedule_text: str,
    prompt: str,
    now_ms: int,
    due_at_ms: int,
) -> tuple[str, ScheduleEntry]:
    """Arm the first fire and persist the schedule; returns its id and stored entry."""
    schedule_id = f"sched-{uuid4().hex}"
    entry = ScheduleEntry(
        schedule_text=schedule_text,
        kind=expr.kind,
        prompt=prompt,
        enabled=True,
        expires_at=_expiry_for(expr, now_ms=now_ms),
        wakeup_id=None,
    )
    entry = replace(entry, wakeup_id=_arm(services, schedule_id=schedule_id, prompt=prompt, due_at_ms=due_at_ms))
    schedules = dict(state.schedules)
    schedules[schedule_id] = entry
    _write_state(services, replace(state, schedules=schedules), expected_version=version)
    services.stage_event("schedule_created", _created_event(schedule_id=schedule_id, entry=entry, expr=expr, next_fire_ms=due_at_ms))
    return schedule_id, entry


# --- Tools ---


CRON_CREATE_DESCRIPTION = (
    "Schedule a prompt to run at a future time — either recurring on an interval or cron schedule, "
    "or once at a specific time. Max 50 schedules per session. Returns the schedule ID and "
    "human-readable cadence."
)

CRON_DELETE_DESCRIPTION = "Cancel a scheduled cron job by ID."

CRON_LIST_DESCRIPTION = "List scheduled cron jobs in this session."


class CronCreateTool:
    """Agent tool to create a scheduled prompt."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_create",
        icon=Icon(glyph="◷", fallback="C"),
        description=CRON_CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "schedule": {
                    "type": "string",
                    "description": (
                        "Schedule expression: 'every <N><s|m|h|d>' (e.g. 'every 10m'), "
                        "'at <ISO-8601>' (e.g. 'at 2026-10-18T10:00:00'), "
                        "'cron <min> <hour> <day> <month> <dow>' (e.g. 'cron */5 * * * *'), or 'once'."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to enqueue and execute on each fire.",
                },
            },
            "required": ["schedule", "prompt"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        context.require_session_id()
        raw_schedule = call.arguments.get("schedule")
        raw_prompt = call.arguments.get("prompt")
        if not isinstance(raw_schedule, str) or not raw_schedule.strip():
            return _failure(self.definition.name, "Schedule expression must be a non-empty string.")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            return _failure(self.definition.name, "Prompt must be a non-empty string.")
        schedule_text = raw_schedule.strip()
        prompt = raw_prompt.strip()
        state, version = _load_state(services)
        if len(state.schedules) >= MAX_SCHEDULES_PER_WORKSPACE:
            return _failure(
                self.definition.name,
                f"Workspace schedule limit reached (maximum {MAX_SCHEDULES_PER_WORKSPACE} jobs). Delete an existing job before scheduling a new one.",
            )
        try:
            expr = parse_schedule(schedule_text)
        except ValueError as exc:
            return _failure(self.definition.name, str(exc))
        now_ms = _clock_ms()
        first_fire_ms = next_fire_after(expr, now_ms // 1000) * 1000
        schedule_id, _entry = _create(
            services,
            state=state,
            version=version,
            expr=expr,
            schedule_text=schedule_text,
            prompt=prompt,
            now_ms=now_ms,
            due_at_ms=first_fire_ms,
        )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps({"id": schedule_id, "human_schedule": human_schedule(expr)}, indent=2)),
        )


class CronDeleteTool:
    """Agent tool to cancel a scheduled prompt by ID."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_delete",
        icon=Icon(glyph="◷", fallback="C"),
        description=CRON_DELETE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Schedule/job ID to cancel.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        context.require_session_id()
        raw_id = call.arguments.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            return _failure(self.definition.name, "Schedule id must be a non-empty string.")
        schedule_id = raw_id.strip()
        state, version = _load_state(services)
        entry = state.schedules.get(schedule_id)
        if entry is None:
            return _failure(self.definition.name, f"Schedule '{schedule_id}' not found.")
        schedules = {key: value for key, value in state.schedules.items() if key != schedule_id}
        _write_state(services, replace(state, schedules=schedules), expected_version=version)
        _disarm(services, entry.wakeup_id)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps({"id": schedule_id}, indent=2)),
        )


class CronListTool:
    """Agent tool to list this session's scheduled prompts."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_list",
        icon=Icon(glyph="◷", fallback="C"),
        description=CRON_LIST_DESCRIPTION,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        replay_policy="safe",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        _ = call
        context.require_session_id()
        state, _version = _load_state(services)
        now_seconds = _clock_ms() // 1000
        jobs: list[dict[str, object]] = []
        for schedule_id, entry in state.schedules.items():
            expr = parse_schedule(entry.schedule_text)
            jobs.append(
                {
                    "id": schedule_id,
                    "schedule_text": entry.schedule_text,
                    "human_schedule": human_schedule(expr),
                    "next_fire_at": next_fire_after(expr, now_seconds) if entry.enabled else None,
                    "enabled": entry.enabled,
                    "expires_at": entry.expires_at,
                }
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps({"jobs": jobs}, indent=2)),
        )


def _failure(tool_name: str, message: str) -> ToolResult:
    return ToolFailure(tool_name=tool_name, error=message, output=TextOutput(text=message))


# --- The /loop command ---


@dataclass(frozen=True, slots=True)
class _LoopSummary:
    """The structural summary ``loop_confirmation_message`` reads (legacy ``ScheduleSummary``)."""

    id: str
    prompt: str
    human_schedule: str


class CronLoopCommand:
    """`/loop [interval] <prompt>`: create an ``every <interval>`` schedule and fire it now."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="loop",
        description="Run a prompt or slash command on a recurring interval, firing it immediately.",
        argument_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Raw /loop arguments: '[interval] <prompt>'."},
            },
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
        client_syntax="/loop",
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw_input = invocation.arguments.get("input")
        interval, prompt = parse_loop_command(raw_input if isinstance(raw_input, str) else "")
        if interval is None or prompt is None:
            return CommandResult(status="ok", output=USAGE_MESSAGE, data={"is_usage": True})
        schedule_text = f"every {interval}"
        try:
            expr = parse_schedule(schedule_text)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        state, version = _load_state(services)
        now_ms = _clock_ms()
        schedule_id, _entry = _create(
            services,
            state=state,
            version=version,
            expr=expr,
            schedule_text=schedule_text,
            prompt=prompt,
            now_ms=now_ms,
            due_at_ms=now_ms,
        )
        summary = _LoopSummary(id=schedule_id, prompt=prompt, human_schedule=human_schedule(expr))
        return CommandResult(
            status="ok",
            output=loop_confirmation_message(summary),
            data={
                "session_id": invocation.context.session_id,
                "schedule_id": schedule_id,
                "schedule": schedule_text,
                "human_schedule": summary.human_schedule,
                "prompt": prompt,
                "next_fire_at": now_ms,
                "is_usage": False,
            },
        )


# --- Observation: the terminal reschedule that prevents overlap and replay ---


def _consume_fired(state: CronState, services: PluginServices, run_id: str) -> tuple[CronState, tuple[str, ...]]:
    """Consume owned, current wakeup facts for the run that just finished."""
    cursor = state.cursor_sequence
    fired: list[str] = []
    for _ in range(_MAX_EVENT_PAGES):
        page = services.read_facts(after_sequence=cursor, limit=_EVENT_PAGE_SIZE)
        for fact in page.facts:
            if not isinstance(fact, WakeupFiredFact) or fact.run_id != run_id or fact.owner != services.owner:
                continue
            if fact.payload.get("kind") != WAKEUP_KIND:
                continue
            schedule_id = fact.payload.get("schedule_id")
            if isinstance(schedule_id, str) and schedule_id:
                entry = state.schedules.get(schedule_id)
                if entry is not None and entry.wakeup_id == fact.wakeup_id and schedule_id not in fired:
                    fired.append(schedule_id)
        cursor = page.next_sequence
        if not page.has_more:
            break
    if cursor == state.cursor_sequence:
        return state, tuple(fired)
    return replace(state, cursor_sequence=cursor), tuple(fired)


def _settle(
    services: PluginServices,
    *,
    schedule_id: str,
    entry: ScheduleEntry,
    status: str,
    now_ms: int,
) -> tuple[str, int | None, ScheduleEntry | None]:
    """One terminal reschedule decision for a fired schedule; ``None`` entry means retired."""
    if entry.kind in _ONE_SHOT_KINDS:
        # One-shot: delete on success; keep DISABLED for inspection on failure (OpenClaw parity).
        if status == "completed":
            _disarm(services, entry.wakeup_id)
            return "deleted", None, None
        return "disabled", None, replace(entry, enabled=False, wakeup_id=None)
    if entry.expires_at is not None and entry.expires_at <= now_ms:
        # Final fire already happened -> the schedule is retired.
        _disarm(services, entry.wakeup_id)
        return "expired", None, None
    # Recompute from NOW: a long run delays the next fire, missed slots are never replayed.
    expr = parse_schedule(entry.schedule_text)
    next_fire_ms = next_fire_after(expr, now_ms // 1000) * 1000
    wakeup_id = _arm(services, schedule_id=schedule_id, prompt=entry.prompt, due_at_ms=next_fire_ms)
    return "rearmed", next_fire_ms, replace(entry, wakeup_id=wakeup_id)


def cron_observer(fact: PluginFact, context: ToolContext, services: PluginServices) -> None:
    """Settle the schedules whose wakeup fired in the run that just finished."""
    _ = context
    if not isinstance(fact, RunFinishedFact):
        return
    stored, version = services.get_state("session")
    state = CronState.from_payload(stored)
    consumed, fired = _consume_fired(state, services, fact.run_id)
    now_ms = _clock_ms()
    schedules = dict(consumed.schedules)
    events: list[dict[str, object]] = []
    for schedule_id in fired:
        entry = schedules.get(schedule_id)
        if entry is None or not entry.enabled:
            # An untracked or already-retired schedule is never re-armed.
            continue
        outcome, next_fire_ms, settled = _settle(
            services,
            schedule_id=schedule_id,
            entry=entry,
            status=fact.status,
            now_ms=now_ms,
        )
        if settled is None:
            del schedules[schedule_id]
        else:
            schedules[schedule_id] = settled
        event: dict[str, object] = {"id": schedule_id, "outcome": outcome, "run_status": fact.status}
        if next_fire_ms is not None:
            event["next_fire_at"] = next_fire_ms
        events.append(event)
    updated = CronState(schedules=schedules, cursor_sequence=consumed.cursor_sequence)
    if updated != state:
        _write_state(services, updated, expected_version=version)
    for event in events:
        services.stage_event("schedule_fired", event)


def prepare_cron_deactivation(
    state: Mapping[str, object],
    scope: str,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: park every schedule disabled with no armed wakeup.

    The activation transaction cancels this owner's pending wakeups, so a kept wakeup id
    would name a dead row and a kept enabled flag would promise fires that cannot happen.
    Schedules stay inspectable after re-enable but never silently re-arm (there is no
    enable path; recreate the schedule to fire again). A claimed wakeup already has its
    run, which the core's running-session blockers govern, not this state copy.
    """
    _ = (scope, scope_key, tip_sequence)
    parsed = CronState.from_payload(state)
    parked = {schedule_id: replace(entry, enabled=False, wakeup_id=None) for schedule_id, entry in parsed.schedules.items()}
    return replace(parsed, schedules=parked).to_payload()


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    # The schedule policy is the ported legacy policy; there is no configuration surface.
    _ = config
    return Plugin(
        tools=(CronCreateTool(), CronDeleteTool(), CronListTool()),
        commands=(CronLoopCommand(),),
        observer=cron_observer,
        observed_facts=("run_finished",),
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "schedules", "cursor_sequence"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "cursor_sequence": {"type": "integer", "minimum": 0},
                    "schedules": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "required": ["schedule_text", "kind", "prompt", "enabled", "expires_at", "wakeup_id"],
                            "additionalProperties": False,
                            "properties": {
                                "schedule_text": {"type": "string"},
                                "kind": {"type": "string", "enum": ["every", "at", "cron", "once"]},
                                "prompt": {"type": "string"},
                                "enabled": {"type": "boolean"},
                                "expires_at": {"type": ["integer", "null"]},
                                "wakeup_id": {"type": ["string", "null"]},
                            },
                        },
                    },
                },
            },
        ),
        prepare_deactivate=prepare_cron_deactivation,
    )
