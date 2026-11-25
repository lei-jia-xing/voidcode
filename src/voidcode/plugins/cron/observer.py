"""Cron fact settlement: durable pending state and the uncapped facts catch-up."""

from __future__ import annotations

from dataclasses import replace

from voidcode.sdk.hooks import PluginFact, RunFinishedFact, WakeupFiredFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

from . import runtime
from .schedule_expr import next_fire_after, parse_schedule
from .scheduling import _arm
from .state import (
    _ONE_SHOT_KINDS,
    CronState,
    PendingFire,
    ScheduleEntry,
    _store,
    _validate_recurring_policy,
)


def _clock_ms() -> int:
    """Observer-local clock seam.

    Unit tests patch the facade, which forwards to :mod:`runtime`; integration tests patch
    ``observer.__globals__['_clock_ms']`` directly. Both paths pin the instant a catch-up
    computes its successor fires for.
    """
    return runtime._clock_ms()


def _stage_state_error(services: PluginServices, error: BaseException | str) -> None:
    """Surface malformed state as a bounded plugin event instead of an observer exception."""
    message = str(error) if isinstance(error, str) else f"{type(error).__name__}: {error}"
    services.stage_event("schedule_error", {"error": message[:500]})


def _disabled_firing(
    state: CronState,
    entry: ScheduleEntry,
    fact: WakeupFiredFact,
    *,
    schedule_id: str,
    reason: str,
    error: str | None = None,
) -> tuple[CronState, dict[str, object]]:
    """Park one malformed/stale owned firing visibly, never as an observer exception."""
    schedules = dict(state.schedules)
    schedules[schedule_id] = replace(entry, enabled=False, wakeup_id=None, next_fire_at=None, pending=None)
    event: dict[str, object] = {
        "id": schedule_id,
        "wakeup_id": fact.wakeup_id,
        "fired_sequence": fact.event_sequence,
        "outcome": "disabled",
        "reason": reason,
        "run_status": None,
    }
    if error is not None:
        event["error"] = error[:300]
    return replace(state, schedules=schedules), event


def _apply_fired(
    state: CronState,
    fact: WakeupFiredFact,
    services: PluginServices,
) -> tuple[CronState, dict[str, object] | None]:
    """Record one owned firing as the schedule's pending settlement."""
    if fact.owner != services.owner:
        return state, None
    if fact.payload.get("kind") != runtime.WAKEUP_KIND:
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
        return _disabled_firing(
            state,
            entry,
            fact,
            schedule_id=schedule_id,
            reason="schedule_error",
            error="cron received a second firing while a settlement was already pending",
        )
    if fact.event_sequence <= state.last_finished_sequence:
        # A late replay after its run already finished cannot be joined to that run. Pause
        # visibly rather than leave an enabled schedule with no armed successor.
        return _disabled_firing(state, entry, fact, schedule_id=schedule_id, reason="settlement_missed")
    try:
        pending = _pending_from_fact(fact, schedule_id=schedule_id, prompt=entry.prompt)
    except ValueError as exc:
        # The owned wakeup id matches, but its immutable payload is not the settlement
        # this schedule recorded. Disable it with the error instead of advancing the
        # cursor past an enabled schedule that can never re-arm.
        return _disabled_firing(state, entry, fact, schedule_id=schedule_id, reason="schedule_error", error=str(exc))
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
    except (ValueError, RuntimeError) as exc:
        # A terminal identity (for example a cancelled row created by an older retry)
        # must not abort the observer and strand the settlement. Park the schedule with
        # a bounded error event instead; the fired claim still settles normally.
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
        page = services.read_facts(after_sequence=cursor, limit=runtime.FACT_PAGE_SIZE)
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
