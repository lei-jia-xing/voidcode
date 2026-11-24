"""Scripted dynamic agent workflows over core-owned children."""

from collections.abc import Mapping
from functools import partial

from voidcode.sdk.plugin import Plugin, StateSchema

from .workflows_plugin import (
    SCHEMA_VERSION,
    WORKFLOW_STATE_SCHEMA,
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
        state=StateSchema(version=SCHEMA_VERSION, schema=WORKFLOW_STATE_SCHEMA),
        prepare_deactivate=prepare_workflows_deactivation,
        background_action=partial(drive_workflow_action, workflows=workflows),
    )
