"""Goal fact observer: usage accounting, blocked audit, and run-finish lifecycle."""

from __future__ import annotations

import time
from dataclasses import replace

from voidcode.sdk.hooks import (
    InvocationFinishedFact,
    PluginFact,
    RunFinishedFact,
    RunRequestedFact,
    UsageFact,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.provider import ProviderTokenUsage
from voidcode.sdk.services import PluginServices

from .budget import _accum_float, _accum_int, _apply_budget, _fact_total_tokens, _get_closed_window_spend
from .continuations import _enqueue_pursuit
from .state import _store, unbound_run_id


def goal_observer(fact: PluginFact, ctx: ToolContext, services: PluginServices) -> None:
    """Observe usage, tool execution, and turn completion facts to update goal state."""
    _ = ctx
    goal, version = _store(services).load()
    if goal is None:
        return

    now = int(time.time() * 1000)

    if isinstance(fact, RunRequestedFact):
        # Bind the goal to the run that is actually pursuing it. A run request that
        # predates the current active window (an old run after replace/resume) is not
        # this goal's run and must never become a terminal-fact authority.
        if goal.is_active and fact.run_id != goal.retired_run_id and any(window.contains(fact.event_sequence) for window in goal.active_windows):
            if goal.active_run_id != fact.run_id:
                updated = replace(goal, active_run_id=fact.run_id, updated_at=now)
                _store(services).save(updated, version)
        return

    if isinstance(fact, UsageFact):
        window = next((w for w in goal.active_windows if w.contains(fact.dispatch_sequence)), None)
        if window is None:
            return
        # A continuation run does not emit a separate run-request fact; its first
        # provider dispatch is the authoritative evidence of the pursuing run. An old
        # run retired by replace/resume is excluded above and cannot re-bind here.
        if goal.is_active and fact.run_id != goal.retired_run_id and fact.run_id != goal.active_run_id:
            goal = replace(goal, active_run_id=fact.run_id)
        # An absent usage report means every token metric is unknown.
        usage = fact.usage if fact.usage is not None else ProviderTokenUsage()
        tokens = _fact_total_tokens(usage)
        updated = replace(
            goal,
            tokens_used=_accum_int(goal.tokens_used, tokens),
            has_unknown_usage=goal.has_unknown_usage or tokens is None,
            time_used_seconds=_accum_float(goal.time_used_seconds, fact.elapsed_seconds),
            cost_usd=_accum_float(goal.cost_usd, fact.cost_usd),
            updated_at=now,
        )
        if window.end_sequence is not None:
            # Late closed-window usage affects lifetime totals. Only an active goal
            # with a current budget needs its open-window allowance tightened.
            open_window = next((w for w in goal.active_windows if w.end_sequence is None), None)
            if goal.is_active and open_window is not None and goal.token_budget is not None:
                # The committed report already includes this fact (including a model-query
                # UsageFact), so the allowance is derived from it once; lifetime totals are
                # not subtracted again.
                closed_spend = _get_closed_window_spend(services, goal.active_windows)
                if closed_spend is None or updated.has_unknown_usage:
                    # Unknown spend cannot fund an invented remaining allowance; set once
                    # per transition since repeat late facts restate identical state.
                    if goal.status != "usage-limited" or goal.window_allowance != 0:
                        _apply_budget(services, since_sequence=open_window.start_sequence, max_total_tokens=0)
                    updated = replace(updated, status="usage-limited", window_allowance=0)
                    services.cancel_continuations()
                    services.stage_event("goal_usage_limited", {"goal_id": goal.goal_id})
                else:
                    allowance = max(0, goal.token_budget - closed_spend)
                    if allowance != goal.window_allowance:
                        _apply_budget(services, since_sequence=open_window.start_sequence, max_total_tokens=allowance)
                    updated = replace(updated, window_allowance=allowance)
                    if goal.window_tokens_used is not None and goal.window_tokens_used >= allowance:
                        updated = replace(updated, status="budget-limited")
                        services.cancel_continuations()
                        services.stage_event(
                            "goal_budget_limited",
                            {"goal_id": goal.goal_id, "window_tokens_used": goal.window_tokens_used, "window_allowance": allowance},
                        )
        else:
            window_tokens = _accum_int(goal.window_tokens_used, tokens)
            status = goal.status
            if goal.window_allowance is not None and goal.status in {"active", "paused", "blocked"}:
                if window_tokens is None:
                    status = "usage-limited"
                elif window_tokens >= goal.window_allowance:
                    status = "budget-limited"
            updated = replace(updated, status=status, window_tokens_used=window_tokens)
            if status != goal.status and status in {"usage-limited", "budget-limited"}:
                services.cancel_continuations()
                payload: dict[str, object] = {"goal_id": goal.goal_id}
                if fact.usage is not None:
                    payload.update(window_tokens_used=window_tokens, window_allowance=goal.window_allowance)
                services.stage_event("goal_budget_limited" if status == "budget-limited" else "goal_usage_limited", payload)
        _store(services).save(updated, version)

    elif isinstance(fact, InvocationFinishedFact):
        if fact.status == "ok" and fact.tool_name not in {"create_goal", "update_goal", "get_goal"}:
            if goal.blocked_turns_count > 0 or goal.last_blocked_logical_turn is not None:
                updated = replace(
                    goal,
                    blocked_turns_count=0,
                    last_blocked_logical_turn=None,
                    updated_at=now,
                )
                _store(services).save(updated, version)

    elif isinstance(fact, RunFinishedFact):
        # Measured work time is accounted per provider dispatch (UsageFact.elapsed_seconds).
        # A run-finished elapsed is neither added (double counting the same work) nor turned
        # into a fabricated 0 when absent, so the accumulated value is preserved as-is.
        stop_cause = fact.stop_cause

        # A terminal fact is authoritative only while its run has not been retired by a
        # goal replacement/resume. Production writers record the immediately replaced
        # run id, so a late old-run limit/user-stop cannot touch a newer goal. A None
        # retired id is the legacy/unbound form used by direct unit fixtures.
        if goal.retired_run_id is not None and fact.run_id == goal.retired_run_id:
            return

        new_blocked_count = goal.blocked_turns_count
        new_last_turn = goal.last_blocked_logical_turn
        if fact.status == "completed" and stop_cause in {"completed", None} and goal.last_blocked_logical_turn != fact.logical_turn:
            new_blocked_count = 0
            new_last_turn = None

        new_status = goal.status
        updated_windows = goal.active_windows
        paused_by_user_stop = stop_cause == "user_stop" and goal.is_active
        paused_by_core_limit = stop_cause == "limit_reached" and goal.is_active
        if paused_by_user_stop or paused_by_core_limit:
            new_status = "paused"
            stop_seq = fact.event_sequence
            updated_windows = tuple(replace(w, end_sequence=stop_seq) if w.end_sequence is None else w for w in goal.active_windows)

        new_active_run_id = goal.active_run_id if new_status == "active" else None
        if new_status == "active":
            # A terminal fact ends the bound run; keep an unbound sentinel rather than
            # null on the active goal so a late old-run fact cannot claim it.
            new_active_run_id = unbound_run_id(goal.goal_id)
        state_changed = (
            new_status != goal.status
            or updated_windows != goal.active_windows
            or new_blocked_count != goal.blocked_turns_count
            or new_last_turn != goal.last_blocked_logical_turn
            or new_active_run_id != goal.active_run_id
        )
        if state_changed:
            updated = replace(
                goal,
                status=new_status,
                active_windows=updated_windows,
                blocked_turns_count=new_blocked_count,
                last_blocked_logical_turn=new_last_turn,
                active_run_id=new_active_run_id,
                updated_at=now,
            )
            if paused_by_user_stop:
                # A real active -> paused transition is the only stop that releases the
                # retained constraint; a paused/complete/limited goal keeps its budget.
                services.clear_budget()
                services.cancel_continuations()
                services.stage_event("goal_paused", {"goal_id": goal.goal_id, "reason": "user_stop"})
            elif paused_by_core_limit:
                # A core/provider/root limit stops automatic pursuit but is not evidence that
                # the user's goal token budget was exhausted. Keep the goal budget intact for
                # edit/resume and record only the source the core actually typed.
                services.cancel_continuations()
                limit_payload: dict[str, object] = {
                    "goal_id": goal.goal_id,
                    "reason": "limit_reached",
                    "stop_cause": stop_cause,
                }
                if fact.budget_exhaustion is not None:
                    limit_payload["budget_exhaustion"] = fact.budget_exhaustion.as_payload()
                services.stage_event("goal_paused", limit_payload)
            _store(services).save(updated, version)
        else:
            # No-op run facts (complete/paused goals, clean active turns) must not churn
            # updated_at or the state version.
            updated = goal

        # Do not auto-enqueue on interrupted/shutdown/owner_cancel/limit/error/failed
        if fact.status != "completed" or stop_cause not in {"completed", None}:
            return

        if updated.is_active:
            _enqueue_pursuit(services, goal_id=goal.goal_id, trigger=f"run:{fact.event_sequence}")
