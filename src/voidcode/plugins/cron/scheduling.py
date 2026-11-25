"""Cron schedule creation, deletion, and staged wakeup arming."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from voidcode.sdk.services import PluginServices, ScheduledWorkServices

from . import runtime
from .schedule_expr import ScheduleExpr, human_schedule
from .state import CronState, ScheduleEntry, _expiry_for, _store, _validate_recurring_policy


def _arm(services: ScheduledWorkServices, *, schedule_id: str, prompt: str, due_at_ms: int) -> str:
    """Stage the next fire of one schedule as a user-role wakeup of the owning session.

    The row commits in the same transaction as the caller's state/events. The idempotency key is
    the schedule plus the exact fire instant, so re-arming the same future point is deterministic
    and a retry can never duplicate a wakeup.
    """
    return services.stage_wakeup(
        {"kind": runtime.WAKEUP_KIND, "text": prompt, "schedule_id": schedule_id},
        due_at_ms=due_at_ms,
        idempotency_key=f"cron:{schedule_id}:{due_at_ms}",
        role="user",
    )


def _disarm(services: ScheduledWorkServices, wakeup_id: str | None) -> None:
    """Stage cancellation of a pending wakeup in the same transaction as the state change."""
    if wakeup_id:
        services.stage_cancel_wakeup(wakeup_id)


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
        "note": runtime.DELETE_IN_FLIGHT_NOTE,
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
    if len(state.schedules) >= runtime.MAX_SCHEDULES_PER_SESSION:
        raise ValueError(
            f"Session schedule limit reached (maximum {runtime.MAX_SCHEDULES_PER_SESSION} jobs). Delete an existing job before scheduling a new one."
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
