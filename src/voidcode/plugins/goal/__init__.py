"""Thread goals: lifecycle tools, ``goal`` commands, views, and accounting."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .goal_plugin import (
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


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
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
        observed_facts=("usage", "invocation_finished", "run_finished"),
        prepare_deactivate=prepare_goal_deactivation,
        close=None,
    )
