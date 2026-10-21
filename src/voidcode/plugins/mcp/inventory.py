"""MCP inventory: the `mcp` command and the bounded discovery context.

The inventory is passive: it reads the manager's configured state and cached
tool surface, so listing servers never spawns a server. Remote annotations stay
display-only.
"""

from __future__ import annotations

from typing import Any, ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

from .types import McpCachedToolSurface, McpManagerState, McpToolDescriptor

#: Servers and tools are bounded so a large remote surface never becomes a wall.
MAX_SERVERS = 32
MAX_TOOLS_PER_SERVER = 50
MAX_CONTEXT_BYTES = 4000


class McpInventoryCommand:
    """List configured MCP servers and their known tools without connecting."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="mcp",
        description="List configured MCP servers and their known tools.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        workspace = invocation.context.require_workspace()
        state = self._manager.current_state()
        surface = self._manager.cached_surface(workspace=workspace)
        servers = _inventory_servers(state, surface)
        return CommandResult(
            status="ok",
            output=_render_inventory(servers),
            data={"servers": servers},
        )


def context_contribution(manager: Any, context: ToolContext) -> ContextContribution | None:
    """Name the configured MCP servers and their known tools for the model."""
    workspace = context.require_workspace()
    state = manager.current_state()
    surface = manager.cached_surface(workspace=workspace)
    servers = _inventory_servers(state, surface)
    if not servers:
        return None
    lines = ["MCP servers (use the `mcp` tool with server and tool names):"]
    for server in servers:
        tools = server["tools"]
        tool_names = ", ".join(tool["name"] for tool in tools) if tools else "no tools discovered yet"
        lines.append(f"- {server['name']} [{server['status']}]: {tool_names}")
    text = "\n".join(lines)
    while len(text.encode("utf-8")) > MAX_CONTEXT_BYTES and len(lines) > 1:
        lines.pop()
        text = "\n".join(lines)
    return ContextContribution(content=text)


def _inventory_servers(state: McpManagerState, surface: McpCachedToolSurface) -> list[dict[str, Any]]:
    configured = state.configuration.servers
    status_by_name = {name: runtime.status for name, runtime in state.servers.items()}
    servers: list[dict[str, Any]] = []
    for name in sorted(set(configured) | set(surface.servers))[:MAX_SERVERS]:
        discovered = surface.servers.get(name, ())
        servers.append(
            {
                "name": name,
                "status": status_by_name.get(name, "configured" if name in configured else "unknown"),
                "tools": [_tool_payload(tool) for tool in discovered[:MAX_TOOLS_PER_SERVER]],
                "tools_known": len(discovered),
            }
        )
    return servers


def _tool_payload(tool: McpToolDescriptor) -> dict[str, object]:
    return {
        "name": tool.tool_name,
        "description": tool.description,
        "enabled": tool.enabled,
        "disabled_reason": tool.disabled_reason,
    }


def _render_inventory(servers: list[dict[str, Any]]) -> str:
    if not servers:
        return "No MCP servers are configured."
    lines = [f"{len(servers)} MCP server(s) configured:"]
    for server in servers:
        tools = server["tools"]
        known = server["tools_known"]
        rendered = ", ".join(str(tool["name"]) for tool in tools) if tools else "none discovered"
        suffix = f" (+{known - len(tools)} more)" if isinstance(known, int) and known > len(tools) else ""
        lines.append(f"- {server['name']} [{server['status']}]: {rendered}{suffix}")
    return "\n".join(lines)
