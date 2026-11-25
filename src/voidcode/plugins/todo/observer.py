"""Fact observation and reminder continuation wiring."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from voidcode.sdk.hooks import InvocationFinishedFact, PluginFact, RunFinishedFact, RunRequestedFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import HistoryServices, PluginServices
from voidcode.sdk.tools import ToolEffect, is_read_tier

from .models import (
    INCOMPLETE_STATUSES,
    TODO_MID_RUN_KIND,
    TODO_REMINDER_KIND,
    ReminderPolicy,
    TodoPhase,
)
from .reminders import (
    TODO_MID_RUN_MAX_PER_CYCLE,
    TODO_REMINDER_ROLE,
    TodoRemindersConfig,
    decide_reminder,
    incomplete_todo_phases,
)
from .state import TodoState, _store

OWN_TOOL_NAMES: frozenset[str] = frozenset({"todo"})
OWNER = "todo"

#: How many event pages one observation may consume while looking for a new user prompt.
_MAX_EVENT_PAGES = 3
_EVENT_PAGE_SIZE = 200


def _sync_cycle(state: TodoState, services: HistoryServices, run_id: str) -> tuple[TodoState, str | None]:
    """Only a fresh user prompt starts a reminder cycle.

    Resumes, queued user follow-ups, and automatic owner continuations retain the
    current budget; todos are session bookkeeping and must not turn self-generated
    work into a new nudge allowance.
    """
    cursor = state.cursor_sequence
    user_run_id: str | None = None
    for _ in range(_MAX_EVENT_PAGES):
        page = services.read_facts(after_sequence=cursor, limit=_EVENT_PAGE_SIZE)
        for fact in page.facts:
            if isinstance(fact, RunRequestedFact) and fact.trigger == "user":
                user_run_id = fact.run_id
        cursor = page.next_sequence
        if not page.has_more:
            break
    if user_run_id is not None:
        fresh = replace(
            state,
            cursor_sequence=cursor,
            cycle_run_id=user_run_id,
            todo_reminder=ReminderPolicy(kind=TODO_REMINDER_KIND, cycle_run_id=user_run_id),
            mid_run=ReminderPolicy(kind=TODO_MID_RUN_KIND, cycle_run_id=user_run_id),
            invocations=0,
        )
        return fresh, user_run_id
    if cursor != state.cursor_sequence:
        return replace(state, cursor_sequence=cursor), state.cycle_run_id or run_id
    return state, state.cycle_run_id or run_id


def _incomplete_count(phases: Sequence[TodoPhase]) -> int:
    return sum(1 for phase in phases for task in phase["tasks"] if task["status"] in INCOMPLETE_STATUSES)


def _account_invocation(state: TodoState, fact: InvocationFinishedFact, cycle: str) -> tuple[TodoState, str | None]:
    mid = state.mid_run if state.mid_run.cycle_run_id == cycle else ReminderPolicy(kind=TODO_MID_RUN_KIND, cycle_run_id=cycle)
    if fact.status == "ok" and fact.tool_name in OWN_TOOL_NAMES and fact.owner == OWNER:
        # A todo touch clears the mutation counter.
        mid = replace(mid, mutations=0, mutation_baseline=0)
    elif fact.status == "ok" and fact.tool_name not in OWN_TOOL_NAMES and not is_read_tier(frozenset(ToolEffect(effect) for effect in fact.effects)):
        mid = replace(mid, mutations=mid.mutations + 1)
    decision = decide_reminder(
        policy=mid,
        max_per_cycle=TODO_MID_RUN_MAX_PER_CYCLE,
        run_id=cycle,
        mutations=mid.mutation_baseline + mid.mutations,
        incomplete_count=_incomplete_count(state.phases),
    )
    updated = replace(state, mid_run=decision.state, cycle_run_id=cycle, invocations=state.invocations + 1)
    return updated, decision.text


def _account_run_finish(state: TodoState, fact: RunFinishedFact, cycle: str, max_per_cycle: int) -> tuple[TodoState, str | None]:
    if fact.status != "completed" or fact.stop_cause not in {"completed", None}:
        return state, None
    reminder = state.todo_reminder if state.todo_reminder.cycle_run_id == cycle else ReminderPolicy(kind=TODO_REMINDER_KIND, cycle_run_id=cycle)
    decision = decide_reminder(
        policy=reminder,
        max_per_cycle=max_per_cycle,
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
    state, version = _store(services).load()
    original = state
    state, cycle = _sync_cycle(state, services, fact.run_id)
    cycle_id = cycle if cycle is not None else fact.run_id
    reminder_type: str | None = None
    attempt = 0
    max_attempts = 0
    incomplete_count = 0
    mutation_count = 0
    reminder: str | None = None
    if isinstance(fact, InvocationFinishedFact):
        state, reminder = _account_invocation(state, fact, cycle_id)
        if reminder is not None:
            mid_state = state.mid_run
            reminder_type = TODO_MID_RUN_KIND
            attempt = mid_state.attempts
            max_attempts = TODO_MID_RUN_MAX_PER_CYCLE
            incomplete_count = _incomplete_count(state.phases)
            mutation_count = mid_state.mutation_baseline
    elif isinstance(fact, RunFinishedFact):
        if fact.stop_cause == "user_stop":
            # A user stop cancels this owner's queued nudges only; goal/cron queues
            # are separate owners and keep their own decisions.
            services.cancel_continuations()
        state, reminder = _account_run_finish(state, fact, cycle_id, config.max_per_cycle)
        if reminder is not None:
            todo_state = state.todo_reminder
            reminder_type = TODO_REMINDER_KIND
            attempt = todo_state.attempts
            max_attempts = config.max_per_cycle
            incomplete_count = _incomplete_count(state.phases)
    else:
        return

    if reminder is not None and reminder_type is not None:
        payload: dict[str, object] = {
            "kind": reminder_type,
            "text": reminder,
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
        _store(services).save(state, version)
