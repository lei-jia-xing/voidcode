"""Thread goals: lifecycle tools, ``goal`` commands, views, and accounting."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin, StateSchema

from .goal_plugin import (
    GOAL_STATE_SCHEMA,
    SCHEMA_VERSION,
    CreateGoalTool,
    GetGoalTool,
    GoalClearCommand,
    GoalCommand,
    GoalDetailsView,
    GoalEditCommand,
    GoalFormView,
    GoalLoadObjectiveCommand,
    GoalPauseCommand,
    GoalResumeCommand,
    GoalSetCommand,
    GoalStatusView,
    UpdateGoalTool,
    goal_context,
    goal_observer,
    prepare_goal_deactivation,
)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    unknown = sorted(set(config or {}) - {"providers"})
    if unknown:
        raise ValueError(f"goal config does not support field: {unknown[0]}")
    tools = (
        GetGoalTool(),
        CreateGoalTool(),
        UpdateGoalTool(),
    )
    commands = (
        GoalCommand(),
        GoalSetCommand(),
        GoalEditCommand(),
        GoalPauseCommand(),
        GoalResumeCommand(),
        GoalClearCommand(),
        GoalLoadObjectiveCommand(),
    )
    views = (
        GoalStatusView(),
        GoalDetailsView(),
        GoalFormView(),
    )

    return Plugin(
        tools=tools,
        commands=commands,
        views=views,
        providers=(),
        context=goal_context,
        observer=goal_observer,
        observed_facts=("usage", "run_requested", "invocation_finished", "run_finished"),
        state=StateSchema(version=SCHEMA_VERSION, schema=GOAL_STATE_SCHEMA),
        prepare_deactivate=prepare_goal_deactivation,
        close=None,
    )
