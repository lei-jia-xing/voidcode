"""The ``task`` tools and subagent monitor commands over core-owned child sessions."""

from collections.abc import Mapping

from voidcode.capabilities.agent_queries import AGENT_DEFINITIONS
from voidcode.sdk.plugin import Plugin, StateSchema

from .agent_reader import AGENT_READER
from .constants import SCHEMA_VERSION
from .monitor import MonitorCancelCommand, MonitorCommand, SubagentStatusView
from .state import SUBAGENTS_STATE_SCHEMA
from .subagents_plugin import TaskBatchTool, TaskTool, WaitTool, agent_definition, prepare_subagents_deactivation


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(
        tools=(TaskTool(), TaskBatchTool(), WaitTool()),
        commands=(MonitorCommand(), MonitorCancelCommand()),
        queries=(AGENT_DEFINITIONS.bind(agent_definition),),
        views=(SubagentStatusView(),),
        resource_readers=(AGENT_READER,),
        state=StateSchema(version=SCHEMA_VERSION, schema=SUBAGENTS_STATE_SCHEMA),
        prepare_deactivate=prepare_subagents_deactivation,
    )
