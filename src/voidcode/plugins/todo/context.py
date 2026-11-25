"""Provider context contribution and hot-deactivation state transform."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import StateScope
from voidcode.sdk.services import PluginServices

from .models import TODO_MID_RUN_KIND, TODO_REMINDER_KIND, ReminderPolicy
from .rendering import render_phases
from .state import TodoState, _store


def todo_context(context: ToolContext, services: PluginServices) -> ContextContribution | None:
    """Contribute the plan truth while active tasks remain."""
    _ = context
    state, _ = _store(services).load()
    contribution = render_phases(state.phases, format="provider")
    return None if contribution is None else ContextContribution(content=contribution)


def prepare_todo_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
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
        todo_reminder=ReminderPolicy(kind=TODO_REMINDER_KIND),
        mid_run=ReminderPolicy(kind=TODO_MID_RUN_KIND),
        invocations=0,
        cycle_run_id=None,
    ).to_payload()
