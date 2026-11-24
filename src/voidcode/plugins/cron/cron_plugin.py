"""Cron plugin: scheduled prompts and recurring task management over core wakeups."""

from __future__ import annotations

import datetime
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import ClassVar, Final, Literal, TypeGuard
from uuid import uuid4

from voidcode.capabilities.plugin_state import PluginStateStore
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import PluginFact, RunFinishedFact, WakeupFiredFact
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.plugin import StateScope
from voidcode.sdk.services import (
    PluginServices,
    ScheduledWorkServices,
    StateServices,
)
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .loop_command import DEFAULT_INTERVAL, USAGE_MESSAGE, loop_confirmation_message, parse_loop_command
from .schedule_expr import ScheduleExpr, human_schedule, next_fire_after, parse_schedule

SCHEMA_VERSION: Final[int] = 2
#: Wakeup payload kind this plugin owns; a fired wakeup of any other kind is not ours.
WAKEUP_KIND: Final[str] = "cron"
#: Claude parity: maximum schedules one session may hold (legacy per-workspace cap).
MAX_SCHEDULES_PER_SESSION: Final[int] = 50
#: Claude parity: 7d default expiration for recurring schedules.
DEFAULT_RECURRING_EXPIRY_SECONDS: Final[int] = 7 * 86400
#: The documented /loop granularity: schedule iterations at most once a minute.
MIN_EVERY_INTERVAL_SECONDS: Final[int] = 60
#: Bounded, user-facing note on the one deliberate delete/claim race.
DELETE_IN_FLIGHT_NOTE: Final[str] = "A run already in flight is not interrupted."

#: One-shot kinds: they fire once and never re-arm.
_ONE_SHOT_KINDS: frozenset[str] = frozenset({"at", "once"})
#: Recurring kinds: they re-arm until their expiry passes.
_RECURRING_KINDS: frozenset[str] = frozenset({"every", "cron"})
_KINDS: frozenset[str] = _ONE_SHOT_KINDS | _RECURRING_KINDS

_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "schedules", "last_finished_sequence", "facts_cursor"})
#: Source events per facts page; catch-up keeps paging until the stream is exhausted.
_FACT_PAGE_SIZE: Final[int] = 200
_SCHEDULE_KEYS: frozenset[str] = frozenset({"schedule_text", "kind", "prompt", "enabled", "expires_at", "wakeup_id", "next_fire_at", "pending"})
_PENDING_KEYS: frozenset[str] = frozenset({"wakeup_id", "fired_sequence", "role", "payload"})
_PENDING_PAYLOAD_KEYS: frozenset[str] = frozenset({"kind", "text", "schedule_id"})

ScheduleKind = Literal["every", "at", "cron", "once"]
PendingRole = Literal["system", "user"]


def _is_schedule_kind(value: object) -> TypeGuard[ScheduleKind]:
    return value in ("every", "at", "cron", "once")


def _clock_ms() -> int:
    """The wall clock the schedule math runs on, in the core's due-time unit.

    The legacy scheduler took this as an injectable ``clock``; it stays a module-level seam so a
    test can pin the instant a fire is computed for.
    """
    return int(time.time() * 1000)


def _human_time(ms: int) -> str:
    """One local timestamp for list output; never used for schedule arithmetic."""
    value = datetime.datetime.fromtimestamp(ms / 1000).astimezone()
    return value.strftime("%Y-%m-%dT%H:%M:%S")


# --- Durable session state ---


@dataclass(frozen=True, slots=True)
class PendingFire:
    """One owned ``wakeup_fired`` fact waiting for its terminal run."""

    wakeup_id: str
    fired_sequence: int
    role: PendingRole
    payload: Mapping[str, object]

    @classmethod
    def from_payload(cls, raw: Mapping[str, object], *, schedule_id: str, prompt: str) -> PendingFire:
        unknown = sorted(str(key) for key in raw if key not in _PENDING_KEYS)
        if unknown:
            raise ValueError(f"cron pending settlement field '{unknown[0]}' is not supported")
        missing = sorted(_PENDING_KEYS - raw.keys())
        if missing:
            raise ValueError("cron pending settlement is missing field(s): " + ", ".join(missing))
        wakeup_id = _required_text(raw["wakeup_id"], field="cron pending settlement wakeup_id")
        fired_sequence = _required_non_negative_int(raw["fired_sequence"], field="cron pending settlement fired_sequence")
        raw_role = raw["role"]
        if raw_role not in ("system", "user"):
            raise ValueError(f"cron pending settlement role must be 'system' or 'user', got {raw_role!r}")
        role: PendingRole = "system" if raw_role == "system" else "user"
        raw_payload = raw["payload"]
        if not isinstance(raw_payload, Mapping):
            raise ValueError("cron pending settlement payload must be an object")
        unknown_payload = sorted(str(key) for key in raw_payload if key not in _PENDING_PAYLOAD_KEYS)
        if unknown_payload:
            raise ValueError(f"cron pending settlement payload field '{unknown_payload[0]}' is not supported")
        missing_payload = sorted(_PENDING_PAYLOAD_KEYS - raw_payload.keys())
        if missing_payload:
            raise ValueError("cron pending settlement payload is missing field(s): " + ", ".join(missing_payload))
        if raw_payload["kind"] != WAKEUP_KIND:
            raise ValueError(f"cron pending settlement payload kind must be {WAKEUP_KIND!r}")
        if raw_payload["schedule_id"] != schedule_id:
            raise ValueError("cron pending settlement payload names a different schedule")
        text = _required_text(raw_payload["text"], field="cron pending settlement payload text")
        if text != prompt:
            raise ValueError("cron pending settlement payload text does not match the schedule prompt")
        return cls(
            wakeup_id=wakeup_id,
            fired_sequence=fired_sequence,
            role=role,
            payload=own_json_object(raw_payload),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "wakeup_id": self.wakeup_id,
            "fired_sequence": self.fired_sequence,
            "role": self.role,
            "payload": json_wire_object(self.payload),
        }


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """One schedule: its expression, prompt, armed wakeup, and in-flight settlement."""

    schedule_text: str
    kind: ScheduleKind
    prompt: str
    enabled: bool
    expires_at: int | None
    wakeup_id: str | None
    next_fire_at: int | None
    pending: PendingFire | None

    @classmethod
    def from_payload(cls, raw: Mapping[str, object], *, schedule_id: str) -> ScheduleEntry:
        unknown = sorted(str(key) for key in raw if key not in _SCHEDULE_KEYS)
        if unknown:
            raise ValueError(f"cron schedule '{schedule_id}' field '{unknown[0]}' is not supported")
        missing = sorted(_SCHEDULE_KEYS - raw.keys())
        if missing:
            raise ValueError(f"cron schedule '{schedule_id}' is missing field(s): " + ", ".join(missing))
        raw_kind = raw["kind"]
        if not _is_schedule_kind(raw_kind):
            raise ValueError(f"cron schedule '{schedule_id}' kind must be one of {sorted(_KINDS)}, got {raw_kind!r}")
        kind = raw_kind
        enabled = raw["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(f"cron schedule '{schedule_id}' enabled must be a boolean")
        schedule_text = _required_text(raw["schedule_text"], field=f"cron schedule '{schedule_id}' schedule_text")
        prompt = _required_text(raw["prompt"], field=f"cron schedule '{schedule_id}' prompt")
        try:
            expr = parse_schedule(schedule_text)
        except ValueError as exc:
            raise ValueError(f"cron schedule '{schedule_id}' has an invalid schedule_text: {exc}") from exc
        if expr.kind != kind:
            raise ValueError(f"cron schedule '{schedule_id}' kind {kind!r} does not match schedule_text {schedule_text!r} ({expr.kind!r})")
        expires_at = _optional_non_negative_int(raw["expires_at"], field=f"cron schedule '{schedule_id}' expires_at")
        wakeup_id = _optional_text(raw["wakeup_id"], field=f"cron schedule '{schedule_id}' wakeup_id")
        next_fire_at = _optional_non_negative_int(raw["next_fire_at"], field=f"cron schedule '{schedule_id}' next_fire_at")
        raw_pending = raw["pending"]
        if raw_pending is not None and not isinstance(raw_pending, Mapping):
            raise ValueError(f"cron schedule '{schedule_id}' pending must be an object or null")
        pending = None if raw_pending is None else PendingFire.from_payload(raw_pending, schedule_id=schedule_id, prompt=prompt)
        if kind in _RECURRING_KINDS:
            if expires_at is None:
                raise ValueError(f"cron schedule '{schedule_id}' recurring schedules require expires_at")
            try:
                _validate_recurring_policy(expr, expires_at=expires_at, next_fire_at=next_fire_at)
            except ValueError as exc:
                raise ValueError(f"cron schedule '{schedule_id}': {exc}") from exc
        elif expires_at is not None:
            raise ValueError(f"cron schedule '{schedule_id}' one-shot schedules cannot expire")
        if pending is not None:
            if not enabled:
                raise ValueError(f"cron schedule '{schedule_id}' cannot be disabled with a pending settlement")
            if wakeup_id != pending.wakeup_id:
                raise ValueError(f"cron schedule '{schedule_id}' pending wakeup does not match its armed wakeup")
            if next_fire_at is not None:
                raise ValueError(f"cron schedule '{schedule_id}' pending settlement must clear next_fire_at")
        elif enabled:
            if wakeup_id is None:
                raise ValueError(f"cron schedule '{schedule_id}' enabled schedules require an armed wakeup")
            if next_fire_at is None:
                raise ValueError(f"cron schedule '{schedule_id}' enabled schedules require next_fire_at")
        elif wakeup_id is not None or next_fire_at is not None:
            raise ValueError(f"cron schedule '{schedule_id}' disabled schedules cannot keep a wakeup or next_fire_at")
        return cls(
            schedule_text=schedule_text,
            kind=kind,
            prompt=prompt,
            enabled=enabled,
            expires_at=expires_at,
            wakeup_id=wakeup_id,
            next_fire_at=next_fire_at,
            pending=pending,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schedule_text": self.schedule_text,
            "kind": self.kind,
            "prompt": self.prompt,
            "enabled": self.enabled,
            "expires_at": self.expires_at,
            "wakeup_id": self.wakeup_id,
            "next_fire_at": self.next_fire_at,
            "pending": None if self.pending is None else self.pending.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class CronState:
    """This plugin's session-scope state: schedules, the last terminal run, and the facts cursor."""

    schedules: Mapping[str, ScheduleEntry] = field(default_factory=dict)
    last_finished_sequence: int = 0
    #: Last source-event sequence covered by a complete catch-up scan. Optional in stored v2 state.
    facts_cursor: int = 0

    @classmethod
    def from_payload(cls, raw: Mapping[str, object]) -> CronState:
        absent = [key for key in ("schema_version", "schedules", "last_finished_sequence") if key not in raw]
        if absent:
            raise ValueError("cron state is missing field(s): " + ", ".join(absent))
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"cron state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        last_finished_sequence = _required_non_negative_int(raw["last_finished_sequence"], field="cron state last_finished_sequence")
        facts_cursor = 0 if "facts_cursor" not in raw else _required_non_negative_int(raw["facts_cursor"], field="cron state facts_cursor")
        raw_schedules = raw["schedules"]
        if not isinstance(raw_schedules, Mapping):
            raise ValueError("cron state schedules must be an object")
        if len(raw_schedules) > MAX_SCHEDULES_PER_SESSION:
            raise ValueError(f"cron state holds {len(raw_schedules)} schedules, exceeding the maximum {MAX_SCHEDULES_PER_SESSION}")
        schedules: dict[str, ScheduleEntry] = {}
        pending_schedule: str | None = None
        for schedule_id, entry in raw_schedules.items():
            if not isinstance(schedule_id, str) or not schedule_id:
                raise ValueError("cron state schedule ids must be non-empty strings")
            if not isinstance(entry, Mapping):
                raise ValueError(f"cron schedule '{schedule_id}' must be an object")
            parsed = ScheduleEntry.from_payload(entry, schedule_id=schedule_id)
            if parsed.pending is not None:
                if pending_schedule is not None:
                    raise ValueError(f"cron state has pending settlements for both '{pending_schedule}' and '{schedule_id}'")
                pending_schedule = schedule_id
                if parsed.pending.fired_sequence <= last_finished_sequence:
                    raise ValueError(f"cron schedule '{schedule_id}' pending settlement is older than the last finished run")
            schedules[schedule_id] = parsed
        return cls(schedules=schedules, last_finished_sequence=last_finished_sequence, facts_cursor=facts_cursor)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "schedules": {schedule_id: entry.to_payload() for schedule_id, entry in self.schedules.items()},
            "last_finished_sequence": self.last_finished_sequence,
            "facts_cursor": self.facts_cursor,
        }


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field)


def _required_non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _optional_non_negative_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _required_non_negative_int(value, field=field)


# --- Wakeup arming (staged atomically with the caller's state and events) ---


def _arm(services: ScheduledWorkServices, *, schedule_id: str, prompt: str, due_at_ms: int) -> str:
    """Stage the next fire of one schedule as a user-role wakeup of the owning session.

    The row commits in the same transaction as the caller's state/events. The idempotency key is
    the schedule plus the exact fire instant, so re-arming the same future point is deterministic
    and a retry can never duplicate a wakeup.
    """
    return services.stage_wakeup(
        {"kind": WAKEUP_KIND, "text": prompt, "schedule_id": schedule_id},
        due_at_ms=due_at_ms,
        idempotency_key=f"cron:{schedule_id}:{due_at_ms}",
        role="user",
    )


def _disarm(services: ScheduledWorkServices, wakeup_id: str | None) -> None:
    """Stage cancellation of a pending wakeup in the same transaction as the state change."""
    if wakeup_id:
        services.stage_cancel_wakeup(wakeup_id)


def _store(services: StateServices) -> PluginStateStore[CronState]:
    """This plugin's session-state store: strict keys, typed parse, CAS save."""
    return PluginStateStore(
        services,
        keys=_STATE_KEYS,
        parse=CronState.from_payload,
        serialize=CronState.to_payload,
        empty=CronState,
        unknown=lambda extra: f"cron state field '{extra[0]}' is not supported",
        # facts_cursor is optional for stored v2 state; the parser enforces the required fields.
        missing=None,
    )


def _expiry_for(expr: ScheduleExpr, *, now_ms: int) -> int | None:
    """Recurring schedules expire after 7 days; one-shots never expire (legacy defaults)."""
    if expr.kind in _RECURRING_KINDS:
        return now_ms + DEFAULT_RECURRING_EXPIRY_SECONDS * 1000
    return None


def _validate_recurring_policy(expr: ScheduleExpr, *, expires_at: int | None, next_fire_at: int | None) -> None:
    """Apply the documented recurring policy to one schedule and its next fire."""
    if expr.kind not in _RECURRING_KINDS:
        return
    if expires_at is None:
        raise ValueError("recurring schedules require an expiry")
    if expr.kind == "every":
        interval = expr.interval_seconds
        if interval is None or interval < MIN_EVERY_INTERVAL_SECONDS:
            raise ValueError(f"'every' interval must be at least 1 minute ({MIN_EVERY_INTERVAL_SECONDS} seconds); got {interval!r}")
        if interval >= DEFAULT_RECURRING_EXPIRY_SECONDS:
            raise ValueError("'every' interval must be shorter than the 7-day recurring expiry; use 'at' or 'once' for a one-shot")
    if next_fire_at is not None and next_fire_at >= expires_at:
        raise ValueError("the next fire is at or after the 7-day recurring expiry; use 'at' or 'once' for a one-shot")


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


def _delete_event(*, schedule_id: str, entry: ScheduleEntry) -> dict[str, object]:
    return {
        "id": schedule_id,
        "wakeup_id": entry.wakeup_id,
        "note": DELETE_IN_FLIGHT_NOTE,
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
    """Stage the first fire and persist the schedule atomically with the caller's result."""
    if len(state.schedules) >= MAX_SCHEDULES_PER_SESSION:
        raise ValueError(
            f"Session schedule limit reached (maximum {MAX_SCHEDULES_PER_SESSION} jobs). Delete an existing job before scheduling a new one."
        )
    expires_at = _expiry_for(expr, now_ms=now_ms)
    _validate_recurring_policy(expr, expires_at=expires_at, next_fire_at=due_at_ms)
    schedule_id = f"sched-{uuid4().hex}"
    wakeup_id = _arm(services, schedule_id=schedule_id, prompt=prompt, due_at_ms=due_at_ms)
    entry = ScheduleEntry(
        schedule_text=schedule_text,
        kind=expr.kind,
        prompt=prompt,
        enabled=True,
        expires_at=expires_at,
        wakeup_id=wakeup_id,
        next_fire_at=due_at_ms,
        pending=None,
    )
    schedules = dict(state.schedules)
    schedules[schedule_id] = entry
    _store(services).save(replace(state, schedules=schedules), version)
    services.stage_event("schedule_created", _created_event(schedule_id=schedule_id, entry=entry, expr=expr, next_fire_ms=due_at_ms))
    return schedule_id, entry


# --- Tools ---


CRON_CREATE_DESCRIPTION = (
    "Schedule a prompt to run at a future time — either recurring on an interval or cron schedule, "
    "or once at a specific time. Max 50 schedules per session. Recurring schedules expire after 7 days; "
    "an 'every' interval must be at least 1 minute and shorter than 7 days. "
    "Returns the schedule ID, human-readable cadence, first fire, and expiry."
)

CRON_DELETE_DESCRIPTION = "Cancel a scheduled cron job by ID. A run already in flight is not interrupted."

CRON_LIST_DESCRIPTION = "List scheduled cron jobs in this session."


class CronCreateTool:
    """Agent tool to create a scheduled prompt."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_create",
        icon=Icon(glyph="◷"),
        description=CRON_CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "schedule": {
                    "type": "string",
                    "description": (
                        "Schedule expression: 'every <N><s|m|h|d>' (minimum 1 minute, shorter than 7 days; e.g. 'every 10m'), "
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
        try:
            state, version = _store(services).load()
        except ValueError as exc:
            return _failure(self.definition.name, f"Cron state is invalid: {exc}")
        try:
            expr = parse_schedule(schedule_text)
            now_ms = _clock_ms()
            first_fire_ms = next_fire_after(expr, now_ms // 1000) * 1000
            schedule_id, entry = _create(
                services,
                state=state,
                version=version,
                expr=expr,
                schedule_text=schedule_text,
                prompt=prompt,
                now_ms=now_ms,
                due_at_ms=first_fire_ms,
            )
        except ValueError as exc:
            return _failure(self.definition.name, str(exc))
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(
                json.dumps(
                    {
                        "id": schedule_id,
                        "human_schedule": human_schedule(expr),
                        "next_fire_at": entry.next_fire_at,
                        "expires_at": entry.expires_at,
                    },
                    indent=2,
                )
            ),
        )


def _delete_schedule(services: PluginServices, schedule_id: str) -> ScheduleEntry:
    state, version = _store(services).load()
    entry = state.schedules.get(schedule_id)
    if entry is None:
        raise ValueError(f"Schedule '{schedule_id}' not found.")
    schedules = {key: value for key, value in state.schedules.items() if key != schedule_id}
    _store(services).save(replace(state, schedules=schedules), version)
    _disarm(services, entry.wakeup_id)
    services.stage_event("schedule_deleted", _delete_event(schedule_id=schedule_id, entry=entry))
    return entry


class CronDeleteTool:
    """Agent tool to cancel a scheduled prompt by ID."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_delete",
        icon=Icon(glyph="◷"),
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
        try:
            _delete_schedule(services, schedule_id)
        except ValueError as exc:
            return _failure(self.definition.name, str(exc))
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps({"id": schedule_id, "note": DELETE_IN_FLIGHT_NOTE}, indent=2)),
        )


class CronListTool:
    """Agent tool to list this session's scheduled prompts."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_list",
        icon=Icon(glyph="◷"),
        description=CRON_LIST_DESCRIPTION,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        replay_policy="safe",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        _ = call
        context.require_session_id()
        try:
            state, _version = _store(services).load()
        except ValueError as exc:
            return _failure(self.definition.name, f"Cron state is invalid: {exc}")
        jobs: list[dict[str, object]] = []
        for schedule_id, entry in state.schedules.items():
            expr = parse_schedule(entry.schedule_text)
            jobs.append(
                {
                    "id": schedule_id,
                    "schedule_text": entry.schedule_text,
                    "human_schedule": human_schedule(expr),
                    "next_fire_at": entry.next_fire_at,
                    "pending_wakeup_id": None if entry.pending is None else entry.pending.wakeup_id,
                    "status": _schedule_status(entry),
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


def _schedule_status(entry: ScheduleEntry) -> str:
    if entry.pending is not None:
        return "running"
    return "enabled" if entry.enabled else "disabled"


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
        text_argument="input",
        description="Run a prompt on a recurring interval (minimum 1 minute), firing it immediately.",
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

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = (invocation, services)
        return CommandPresentation(
            title="Schedule a repeating prompt",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "input",
                        "label": (f"[interval] prompt — minimum 1m, e.g. 5m check the deploy; default {DEFAULT_INTERVAL}"),
                        "field_type": "multiline",
                        "required": True,
                    }
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw_input = invocation.arguments.get("input")
        interval, prompt = parse_loop_command(raw_input if isinstance(raw_input, str) else "")
        if interval is None or prompt is None:
            return CommandResult(status="ok", output=USAGE_MESSAGE, data={"is_usage": True}, presentation=self.prepare_input(invocation, services))
        schedule_text = f"every {interval}"
        try:
            expr = parse_schedule(schedule_text)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        try:
            state, version = _store(services).load()
        except ValueError as exc:
            return CommandResult(status="error", error=f"Cron state is invalid: {exc}")
        now_ms = _clock_ms()
        try:
            schedule_id, entry = _create(
                services,
                state=state,
                version=version,
                expr=expr,
                schedule_text=schedule_text,
                prompt=prompt,
                now_ms=now_ms,
                due_at_ms=now_ms,
            )
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
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
                "next_fire_at": entry.next_fire_at,
                "expires_at": entry.expires_at,
                "is_usage": False,
            },
            presentation=CommandPresentation(
                title="Repeating prompt created",
                view_type="markdown",
                data={
                    "content": (
                        f"{summary.human_schedule}\n\n{prompt[:1800]}\n\n"
                        f"Schedule ID: {schedule_id}\nExpires: {_human_time(entry.expires_at) if entry.expires_at is not None else 'never'}"
                    ),
                    "actions": [
                        {"label": "Browse schedules", "action": "loop.list"},
                        {"label": "Cancel this schedule", "action": "loop.delete", "arguments": {"id": schedule_id}},
                    ],
                },
            ),
        )


class CronListCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="loop.list",
        description="Browse this session's scheduled prompts.",
        argument_schema={"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        try:
            state, _ = _store(services).load()
        except ValueError as exc:
            return CommandResult(status="error", error=f"Cron state is invalid: {exc}")
        entries = list(state.schedules.items())
        offset = invocation.arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(entries):
            return CommandResult(status="error", error="Schedule list changed; reopen /loop list.")
        lines = [f"Schedules {offset}–{min(offset + 10, len(entries))} of {len(entries)}"]
        actions: list[dict[str, object]] = [{"label": "Create repeating prompt", "action": "loop", "prepare_input": True}]
        if not entries:
            lines.append("No scheduled prompts. Create one to begin.")
        for identifier, entry in entries[offset : offset + 10]:
            detail = f"{_schedule_status(entry)}"
            if entry.next_fire_at is not None:
                detail += f" · next {_human_time(entry.next_fire_at)}"
            if entry.expires_at is not None:
                detail += f" · expires {_human_time(entry.expires_at)}"
            lines.append(f"{identifier} · {detail} · {entry.schedule_text[:100]}\n{entry.prompt[:200]}")
            actions.append({"label": f"Cancel: {entry.prompt[:40]}", "action": "loop.delete", "arguments": {"id": identifier}})
        if offset:
            actions.append({"label": "Previous schedules", "action": "loop.list", "arguments": {"offset": max(0, offset - 10)}})
        if offset + 10 < len(entries):
            actions.append({"label": "Next schedules", "action": "loop.list", "arguments": {"offset": offset + 10}})
        return CommandResult(
            status="ok",
            output="\n\n".join(lines),
            data={"total": len(entries)},
            presentation=CommandPresentation(
                title="Scheduled prompts", view_type="markdown", data={"content": "\n\n".join(lines), "actions": actions}
            ),
        )


class CronDeleteCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="loop.delete",
        description="Cancel a scheduled prompt by ID. A run already in flight is not interrupted.",
        text_argument="id",
        argument_schema={
            "type": "object",
            "properties": {"id": {"type": "string", "minLength": 1}},
            "required": ["id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        identifier = invocation.arguments.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            return CommandResult(status="error", error="Choose a schedule from /loop list.")
        try:
            _delete_schedule(services, identifier.strip())
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        listed = CronListCommand().invoke(CommandInvocation(invocation.context, "loop.list"), services)
        return CommandResult(
            status="ok",
            output=f"Schedule {identifier} cancelled. {DELETE_IN_FLIGHT_NOTE}",
            data={"id": identifier},
            presentation=listed.presentation,
        )


# --- Observation: settle from wakeup facts, never from a bounded history scan ---


def _stage_state_error(services: PluginServices, error: BaseException | str) -> None:
    """Surface malformed state as a bounded plugin event instead of an observer exception."""
    message = str(error) if isinstance(error, str) else f"{type(error).__name__}: {error}"
    services.stage_event("schedule_error", {"error": message[:500]})


def _apply_fired(
    state: CronState,
    fact: WakeupFiredFact,
    services: PluginServices,
) -> tuple[CronState, dict[str, object] | None]:
    """Record one owned firing as the schedule's pending settlement."""
    if fact.owner != services.owner:
        return state, None
    if fact.payload.get("kind") != WAKEUP_KIND:
        return state, None
    schedule_id = fact.payload.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id:
        return state, None
    entry = state.schedules.get(schedule_id)
    if entry is None or not entry.enabled:
        return state, None
    if entry.wakeup_id != fact.wakeup_id:
        return state, None
    if entry.pending is not None:
        if entry.pending.wakeup_id == fact.wakeup_id and entry.pending.fired_sequence == fact.event_sequence:
            return state, None
        raise ValueError("cron received a second firing while a settlement was already pending")
    pending = _pending_from_fact(fact, schedule_id=schedule_id, prompt=entry.prompt)
    if fact.event_sequence <= state.last_finished_sequence:
        # A late replay after its run already finished cannot be joined to that run. Pause
        # visibly rather than leave an enabled schedule with no armed successor.
        disabled = replace(entry, enabled=False, wakeup_id=None, next_fire_at=None, pending=None)
        schedules = dict(state.schedules)
        schedules[schedule_id] = disabled
        return (
            replace(state, schedules=schedules),
            {
                "id": schedule_id,
                "wakeup_id": fact.wakeup_id,
                "fired_sequence": fact.event_sequence,
                "outcome": "disabled",
                "reason": "settlement_missed",
                "run_status": None,
            },
        )
    running = replace(entry, next_fire_at=None, pending=pending)
    schedules = dict(state.schedules)
    schedules[schedule_id] = running
    return replace(state, schedules=schedules), None


def _pending_from_fact(fact: WakeupFiredFact, *, schedule_id: str, prompt: str) -> PendingFire:
    return PendingFire.from_payload(
        {
            "wakeup_id": fact.wakeup_id,
            "fired_sequence": fact.event_sequence,
            "role": fact.role,
            "payload": fact.payload,
        },
        schedule_id=schedule_id,
        prompt=prompt,
    )


def _settle(
    services: PluginServices,
    *,
    schedule_id: str,
    entry: ScheduleEntry,
    fact: RunFinishedFact,
    now_ms: int,
) -> tuple[str, int | None, ScheduleEntry | None, dict[str, object]]:
    """One terminal reschedule decision for a fired schedule; ``None`` entry means retired."""
    pending = entry.pending
    if pending is None:
        raise ValueError("settlement requires a pending firing")
    event: dict[str, object] = {
        "id": schedule_id,
        "wakeup_id": pending.wakeup_id,
        "fired_sequence": pending.fired_sequence,
        "run_status": fact.status,
    }
    if fact.budget_exhaustion is not None:
        event["budget_exhaustion"] = fact.budget_exhaustion.as_payload()
    limit_stopped = fact.budget_exhaustion is not None or fact.stop_cause == "limit_reached"
    if entry.kind in _ONE_SHOT_KINDS:
        # One-shot: delete on success; keep DISABLED for inspection on failure (OpenClaw parity).
        if fact.status == "completed" and not limit_stopped:
            return "deleted", None, None, event
        event["reason"] = "limit" if limit_stopped else "run_failed"
        return "disabled", None, replace(entry, enabled=False, wakeup_id=None, next_fire_at=None, pending=None), event
    if limit_stopped:
        # A capped recurring run must not re-arm into the same exhausted limit forever.
        event["reason"] = "limit"
        return "disabled", None, replace(entry, enabled=False, wakeup_id=None, next_fire_at=None, pending=None), event
    if entry.expires_at is not None and entry.expires_at <= now_ms:
        event["reason"] = "expiry"
        return "expired", None, None, event
    try:
        expr = parse_schedule(entry.schedule_text)
        next_fire_ms = next_fire_after(expr, now_ms // 1000) * 1000
        if entry.expires_at is not None and next_fire_ms >= entry.expires_at:
            event["reason"] = "expiry"
            return "expired", None, None, event
        _validate_recurring_policy(expr, expires_at=entry.expires_at, next_fire_at=None)
        wakeup_id = _arm(services, schedule_id=schedule_id, prompt=entry.prompt, due_at_ms=next_fire_ms)
    except ValueError as exc:
        event["reason"] = "schedule_error"
        event["error"] = str(exc)[:300]
        return "disabled", None, replace(entry, enabled=False, wakeup_id=None, next_fire_at=None, pending=None), event
    event["next_fire_at"] = next_fire_ms
    return (
        "rearmed",
        next_fire_ms,
        replace(entry, wakeup_id=wakeup_id, next_fire_at=next_fire_ms, pending=None),
        event,
    )


def _apply_finished(
    state: CronState,
    fact: RunFinishedFact,
    services: PluginServices,
    now_ms: int,
) -> tuple[CronState, dict[str, object] | None]:
    """Settle one pending firing on the terminal run that just finished."""
    schedules = dict(state.schedules)
    event: dict[str, object] | None = None
    for schedule_id, entry in state.schedules.items():
        pending = entry.pending
        if pending is None or pending.fired_sequence >= fact.event_sequence:
            continue
        try:
            outcome, _next_fire_ms, settled, payload = _settle(
                services,
                schedule_id=schedule_id,
                entry=entry,
                fact=fact,
                now_ms=now_ms,
            )
        except ValueError as exc:
            _stage_state_error(services, exc)
            return state, None
        if settled is None:
            del schedules[schedule_id]
        else:
            schedules[schedule_id] = settled
        event = {"outcome": outcome, **payload}
        break
    new_last = max(state.last_finished_sequence, fact.event_sequence)
    if new_last == state.last_finished_sequence and not event and schedules == dict(state.schedules):
        return state, None
    return replace(state, schedules=schedules, last_finished_sequence=new_last), event


def _catch_up(services: PluginServices) -> None:
    """Page facts since the cursor to exhaustion and commit cursor + state atomically.

    The cursor advances only after every page has been consumed. Any raised observer
    failure or state CAS leaves it untouched, so the next writable fact callback retries
    the same range. The observer is only the trigger; it never processes facts directly.
    """
    try:
        state, version = _store(services).load()
    except ValueError as exc:
        _stage_state_error(services, exc)
        return
    if not state.schedules:
        return
    cursor = state.facts_cursor
    updated = state
    events: list[dict[str, object]] = []
    now_ms = _clock_ms()
    while True:
        page = services.read_facts(after_sequence=cursor, limit=_FACT_PAGE_SIZE)
        for fact in page.facts:
            try:
                if isinstance(fact, WakeupFiredFact):
                    updated, event = _apply_fired(updated, fact, services)
                elif isinstance(fact, RunFinishedFact):
                    updated, event = _apply_finished(updated, fact, services, now_ms)
                else:
                    continue
            except ValueError as exc:
                _stage_state_error(services, exc)
                continue
            if event is not None:
                events.append(event)
        if page.next_sequence <= cursor and page.has_more:
            raise RuntimeError("cron facts page made no progress; cursor left unadvanced")
        cursor = page.next_sequence
        if not page.has_more:
            break
    updated = replace(updated, facts_cursor=cursor)
    if updated == state:
        return
    _store(services).save(updated, version)
    for event in events:
        services.stage_event("schedule_fired", event)


def cron_observer(fact: PluginFact, context: ToolContext, services: PluginServices) -> None:
    """Trigger a full facts catch-up for every delivered run start, wakeup, or terminal fact."""
    _ = (fact, context)
    _catch_up(services)


def prepare_cron_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
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
    parked = {
        schedule_id: replace(entry, enabled=False, wakeup_id=None, next_fire_at=None, pending=None) for schedule_id, entry in parsed.schedules.items()
    }
    return replace(parsed, schedules=parked).to_payload()
