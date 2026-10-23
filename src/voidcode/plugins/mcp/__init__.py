"""MCP plugin: one ``mcp`` tool backed by the plugin-owned MCP manager."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import PluginServices

from .inventory import McpInventoryCommand, context_contribution
from .mcp_manager import build_mcp_manager
from .mcp_tool import McpTool
from .types import McpConfig


def _mcp_config(config: Mapping[str, object] | None) -> McpConfig:
    raw = {key: value for key, value in (config or {}).items() if key != "providers"}
    return McpConfig.model_validate(json_wire_object(raw))


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    manager = build_mcp_manager(_mcp_config(config))

    def contribute(context: ToolContext, services: PluginServices) -> ContextContribution | None:
        _ = services
        return context_contribution(manager, context)

    return Plugin(
        tools=(McpTool(manager=manager),),
        commands=(McpInventoryCommand(manager),),
        context=contribute,
        close=manager.shutdown,
    )
