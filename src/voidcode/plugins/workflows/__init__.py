"""Scripted dynamic agent workflows over core-owned children."""

from collections.abc import Mapping
from functools import partial

from voidcode.sdk.plugin import Plugin

from .workflows_plugin import (
    WorkflowCancelCommand,
    WorkflowPauseCommand,
    WorkflowResumeCommand,
    WorkflowRunCommand,
    WorkflowRunFormView,
    WorkflowRunTool,
    WorkflowStatusCommand,
    WorkflowStatusView,
    drive_workflow_action,
    parse_workflows_config,
    prepare_workflows_deactivation,
)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    workflows = parse_workflows_config(config)
    return Plugin(
        tools=(WorkflowRunTool(workflows),),
        commands=(
            WorkflowRunCommand(workflows),
            WorkflowStatusCommand(),
            WorkflowPauseCommand(),
            WorkflowResumeCommand(workflows),
            WorkflowCancelCommand(),
        ),
        views=(WorkflowStatusView(), WorkflowRunFormView(workflows)),
        prepare_deactivate=prepare_workflows_deactivation,
        background_action=partial(drive_workflow_action, workflows=workflows),
    )
