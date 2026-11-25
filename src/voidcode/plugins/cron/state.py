"""Cron session state: typed models, strict parsing, policy, and the CAS store."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, TypeGuard

from voidcode.capabilities.plugin_state import PluginStateStore
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.services import StateServices

from . import runtime
from .schedule_expr import ScheduleExpr, parse_schedule

SCHEMA_VERSION: Final[int] = 2

#: One-shot kinds: they fire once and never re-arm.
_ONE_SHOT_KINDS: frozenset[str] = frozenset({"at", "once"})
#: Recurring kinds: they re-arm until their expiry passes.
_RECURRING_KINDS: frozenset[str] = frozenset({"every", "cron"})
_KINDS: frozenset[str] = _ONE_SHOT_KINDS | _RECURRING_KINDS

_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "schedules", "last_finished_sequence", "facts_cursor"})
_SCHEDULE_KEYS: frozenset[str] = frozenset({"schedule_text", "kind", "prompt", "enabled", "expires_at", "wakeup_id", "next_fire_at", "pending"})
_PENDING_KEYS: frozenset[str] = frozenset({"wakeup_id", "fired_sequence", "role", "payload"})
_PENDING_PAYLOAD_KEYS: frozenset[str] = frozenset({"kind", "text", "schedule_id"})

ScheduleKind = Literal["every", "at", "cron", "once"]
PendingRole = Literal["system", "user"]


def _is_schedule_kind(value: object) -> TypeGuard[ScheduleKind]:
    return value in ("every", "at", "cron", "once")


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
        if raw_payload["kind"] != runtime.WAKEUP_KIND:
            raise ValueError(f"cron pending settlement payload kind must be {runtime.WAKEUP_KIND!r}")
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
        if len(raw_schedules) > runtime.MAX_SCHEDULES_PER_SESSION:
            raise ValueError(f"cron state holds {len(raw_schedules)} schedules, exceeding the maximum {runtime.MAX_SCHEDULES_PER_SESSION}")
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
        return now_ms + runtime.DEFAULT_RECURRING_EXPIRY_SECONDS * 1000
    return None


def _validate_recurring_policy(expr: ScheduleExpr, *, expires_at: int | None, next_fire_at: int | None) -> None:
    """Apply the documented recurring policy to one schedule and its next fire."""
    if expr.kind not in _RECURRING_KINDS:
        return
    if expires_at is None:
        raise ValueError("recurring schedules require an expiry")
    if expr.kind == "every":
        interval = expr.interval_seconds
        if interval is None or interval < runtime.MIN_EVERY_INTERVAL_SECONDS:
            raise ValueError(f"'every' interval must be at least 1 minute ({runtime.MIN_EVERY_INTERVAL_SECONDS} seconds); got {interval!r}")
        if interval >= runtime.DEFAULT_RECURRING_EXPIRY_SECONDS:
            raise ValueError("'every' interval must be shorter than the 7-day recurring expiry; use 'at' or 'once' for a one-shot")
    if next_fire_at is not None and next_fire_at >= expires_at:
        raise ValueError("the next fire is at or after the 7-day recurring expiry; use 'at' or 'once' for a one-shot")
