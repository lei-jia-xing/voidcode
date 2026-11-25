"""Goal deactivation transform: close the live window and pause."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from voidcode.capabilities.plugin_state import require_keys
from voidcode.sdk.plugin import StateScope

from .state import (
    DEACTIVATABLE_GOAL_STATUSES,
    KNOWN_GOAL_STATE_FIELDS,
    GoalState,
    _unknown_state_field,
)


def prepare_goal_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Close the live window at the tip and pause the goal.

    Session scope needs its event tip; workspace scope needs none. A tip before an
    open window's start is refused, so no inverted range is ever written.
    """
    _ = scope_key
    if scope == "session":
        if isinstance(tip_sequence, bool) or not isinstance(tip_sequence, int) or tip_sequence < 0:
            raise ValueError(f"Session-scope goal deactivation requires the owning session's event tip as a non-negative integer: {tip_sequence!r}")
    elif scope == "workspace":
        if tip_sequence is not None:
            raise ValueError(f"Workspace-scope goal deactivation has no single session event tip: {tip_sequence!r}")
    else:
        raise ValueError(f"Unknown goal state scope: {scope!r}")

    require_keys(state, KNOWN_GOAL_STATE_FIELDS, unknown=_unknown_state_field)
    goal = GoalState.from_dict(state)
    if goal is None:
        return state

    open_windows = tuple(window for window in goal.active_windows if window.end_sequence is None)
    if not open_windows and goal.status not in DEACTIVATABLE_GOAL_STATUSES:
        return goal.to_dict()
    if tip_sequence is None:
        raise ValueError("Cannot close an open goal window without the owning session's event tip")
    if any(window.start_sequence > tip_sequence for window in open_windows):
        raise ValueError(f"Goal window starts after the session event tip {tip_sequence}: the window cannot close at an earlier point")

    prepared = replace(
        goal,
        status="paused" if goal.status in DEACTIVATABLE_GOAL_STATUSES else goal.status,
        active_windows=tuple(replace(window, end_sequence=tip_sequence) if window.end_sequence is None else window for window in goal.active_windows),
    )
    return prepared.to_dict()
