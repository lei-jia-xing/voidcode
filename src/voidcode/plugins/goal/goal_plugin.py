"""Stable facade for the goal plugin.

The implementation is split into cohesive modules: ``state`` (schema, typed state,
strict parsing, and the CAS store), ``objective`` (inline/artifact objectives),
``context`` (prompt framing and the user-role contribution), ``budget`` (Core
constraint staging and window spend), ``continuations`` (the single automatic
pursuit), ``tool``, ``commands``, ``views``, ``observer``, and ``lifecycle``.

Modules depend only downward, and this module is the single import surface for the
plugin factory, tests, and historical callers. No behavior lives here; the
pre-split private names remain importable because the existing suites exercise
them directly.
"""

from __future__ import annotations

# ruff: noqa: F401 - compatibility re-exports are the point of this facade.
from .budget import (
    _accum_float,
    _accum_int,
    _apply_budget,
    _fact_total_tokens,
    _get_closed_window_spend,
)
from .commands import (
    GoalClearCommand,
    GoalCommand,
    GoalEditCommand,
    GoalLoadObjectiveCommand,
    GoalPauseCommand,
    GoalResumeCommand,
    GoalSetCommand,
    _goal_presentation,
)
from .context import (
    _CONTEXT_HEADLINE,
    _EDITED_HEADLINE,
    _PURSUE_HEADLINE,
    _prompt_budget_section,
    _prompt_objective_block,
    goal_context,
    render_context_prompt,
)
from .continuations import _enqueue_pursuit
from .lifecycle import prepare_goal_deactivation
from .objective import (
    OBJECTIVE_INLINE_CONTEXT_CHARS,
    read_full_objective,
    spill_objective_if_needed,
)
from .observer import goal_observer
from .state import (
    DEACTIVATABLE_GOAL_STATUSES,
    GOAL_STATE_SCHEMA,
    KNOWN_GOAL_STATE_FIELDS,
    SCHEMA_VERSION,
    VALID_GOAL_STATUSES,
    ActiveWindow,
    GoalState,
    GoalStatus,
    _parse_goal_artifact_ref,
    _parse_goal_metric_float,
    _parse_goal_metric_int,
    _parse_goal_windows,
    _store,
    _unknown_state_field,
    unbound_run_id,
)
from .tool import (
    CreateGoalTool,
    GetGoalTool,
    GoalResultBody,
    UpdateGoalTool,
)
from .views import GoalDetailsView, GoalFormView, GoalStatusView

__all__ = [
    "DEACTIVATABLE_GOAL_STATUSES",
    "GOAL_STATE_SCHEMA",
    "KNOWN_GOAL_STATE_FIELDS",
    "OBJECTIVE_INLINE_CONTEXT_CHARS",
    "SCHEMA_VERSION",
    "VALID_GOAL_STATUSES",
    "ActiveWindow",
    "CreateGoalTool",
    "GetGoalTool",
    "GoalClearCommand",
    "GoalCommand",
    "GoalDetailsView",
    "GoalEditCommand",
    "GoalFormView",
    "GoalLoadObjectiveCommand",
    "GoalPauseCommand",
    "GoalResultBody",
    "GoalResumeCommand",
    "GoalSetCommand",
    "GoalState",
    "GoalStatus",
    "GoalStatusView",
    "UpdateGoalTool",
    "unbound_run_id",
    "goal_context",
    "goal_observer",
    "prepare_goal_deactivation",
    "read_full_objective",
    "render_context_prompt",
    "spill_objective_if_needed",
]

#: Historical private aliases used by focused tests and diagnostics.
__all__.extend(
    [
        "_CONTEXT_HEADLINE",
        "_EDITED_HEADLINE",
        "_PURSUE_HEADLINE",
        "_accum_float",
        "_accum_int",
        "_apply_budget",
        "_enqueue_pursuit",
        "_fact_total_tokens",
        "_get_closed_window_spend",
        "_goal_presentation",
        "_parse_goal_artifact_ref",
        "_parse_goal_metric_float",
        "_parse_goal_metric_int",
        "_parse_goal_windows",
        "_prompt_budget_section",
        "_prompt_objective_block",
        "_store",
        "_unknown_state_field",
    ]
)
