"""The ``task`` tools and ``agents`` commands over core-owned child sessions."""

from collections.abc import Mapping

from voidcode.capabilities.agent_queries import AGENT_DEFINITIONS
from voidcode.sdk.plugin import Plugin

from .delegation_plugin import TaskBatchTool, TaskTool, agent_definition, prepare_delegation_deactivation
from .profiles.commands import AgentsCommand, AgentShowCommand


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(
        tools=(TaskTool(), TaskBatchTool()),
        commands=(AgentsCommand(), AgentShowCommand()),
        queries=(AGENT_DEFINITIONS.bind(agent_definition),),
        prepare_deactivate=prepare_delegation_deactivation,
    )
