"""The ``task`` tools and subagent monitor commands over core-owned child sessions."""

from collections.abc import Mapping

from voidcode.capabilities.agent_queries import AGENT_DEFINITIONS
from voidcode.sdk.plugin import Plugin

from .monitor import MonitorCancelCommand, MonitorCommand
from .subagents_plugin import TaskBatchTool, TaskTool, agent_definition, prepare_subagents_deactivation


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(
        tools=(TaskTool(), TaskBatchTool()),
        commands=(MonitorCommand(), MonitorCancelCommand()),
        queries=(AGENT_DEFINITIONS.bind(agent_definition),),
        prepare_deactivate=prepare_subagents_deactivation,
    )
