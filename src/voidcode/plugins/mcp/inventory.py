"""MCP inventory: the `mcp` command and the bounded discovery context.

The inventory is passive: it reads the manager's configured state and cached
tool surface, so listing servers never spawns a server. Remote annotations stay
display-only.
"""

from __future__ import annotations

from typing import Any, ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

from .types import McpCachedToolSurface, McpManager, McpManagerState, McpToolDescriptor

#: Servers and tools are bounded so a large remote surface never becomes a wall.
MAX_SERVERS = 32
MAX_TOOLS_PER_SERVER = 50
MAX_CONTEXT_BYTES = 4000


class McpInventoryCommand:
    """List configured MCP servers and their known tools without connecting."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="mcp",
        description="List configured MCP servers and their known tools.",
        argument_schema={
            "type": "object",
            "properties": {"server": {"type": "string", "minLength": 1}, "offset": {"type": "integer", "minimum": 0}},
            "additionalProperties": False,
        },
        text_argument="server",
    )

    def __init__(self, manager: McpManager) -> None:
        self._manager = manager

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        workspace = invocation.context.require_workspace()
        state = self._manager.current_state()
        surface = self._manager.cached_surface(workspace=workspace)
        servers = _inventory_servers(state, surface)
        selected = invocation.arguments.get("server")
        if selected is not None:
            server = next((row for row in servers if row["name"] == selected), None)
            if server is None:
                return CommandResult(status="error", error=f"Unknown MCP server: {selected}. Reopen /mcp to choose a configured server.")
            offset = invocation.arguments.get("offset", 0)
            tools = surface.servers.get(server["name"], ())
            if type(offset) is not int or not 0 <= offset <= len(tools):
                return CommandResult(status="error", error="Tool offset is beyond the cached inventory; reopen /mcp.")
            known = server["tools_known"]
            lines = [f"# {server['name'][:200]}", f"Status: {server['status']}", f"Tools: {'not discovered' if known is None else known}"]
            if server["error"]:
                lines.append(f"Error: {str(server['error'])[:1500]}")
            for descriptor in tools[offset : offset + 10]:
                tool = _tool_payload(descriptor)
                status = "enabled" if tool["enabled"] else "disabled"
                lines.append(f"- {str(tool['name'])[:120]} [{status}]: {str(tool['description'] or '')[:120]}")
                if tool["disabled_reason"]:
                    lines.append(f"  Reason: {str(tool['disabled_reason'])[:120]}")
            actions: list[dict[str, object]] = [{"label": "Back to servers", "action": "mcp"}]
            if offset:
                actions.append({"label": "Previous tools", "action": "mcp", "arguments": {"server": selected, "offset": max(0, offset - 10)}})
            if offset + 10 < len(tools):
                actions.append({"label": "Next tools", "action": "mcp", "arguments": {"server": selected, "offset": offset + 10}})
            if isinstance(known, int):
                lines.append(f"Showing tools {offset}–{min(offset + 10, known)} of {known}.")
            return CommandResult(
                status="ok",
                output="\n".join(lines),
                data={"server": server},
                presentation=CommandPresentation(
                    title="MCP server details",
                    view_type="markdown",
                    data={"content": "\n\n".join(lines), "actions": actions},
                ),
            )
        if not servers:
            presentation = CommandPresentation(
                title="MCP servers",
                view_type="notice",
                data={"message": "No MCP servers are configured. Configure servers in voidcode.json, then run /core reload."},
            )
        else:
            presentation = CommandPresentation(
                title="MCP servers",
                view_type="choices",
                data={
                    "options": [{"id": row["name"], "label": f"{row['name'][:160]} · {row['status']}"} for row in servers],
                    "action": "mcp",
                    "argument_name": "server",
                },
            )
        return CommandResult(
            status="ok",
            output=_render_inventory(servers),
            data={"servers": servers},
            presentation=presentation,
        )


def context_contribution(manager: McpManager, context: ToolContext) -> ContextContribution | None:
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
        tool_names = ", ".join(tool["name"] for tool in tools) if tools else "no tools" if server["tools_known"] == 0 else "not discovered"
        lines.append(f"- {server['name']} [{server['status']}]: {tool_names}")
    text = "\n".join(lines)
    while len(text.encode("utf-8")) > MAX_CONTEXT_BYTES and len(lines) > 1:
        lines.pop()
        text = "\n".join(lines)
    return ContextContribution(content=text)


def _inventory_servers(state: McpManagerState, surface: McpCachedToolSurface) -> list[dict[str, Any]]:
    configured = state.configuration.servers
    servers: list[dict[str, Any]] = []
    for name in sorted(set(configured) | set(surface.servers))[:MAX_SERVERS]:
        discovered = surface.servers.get(name, ())
        runtime = state.servers.get(name)
        servers.append(
            {
                "name": name,
                "status": runtime.status if runtime is not None else "configured" if name in configured else "unknown",
                "error": runtime.error if runtime is not None else None,
                "tools": [_tool_payload(tool) for tool in discovered[:MAX_TOOLS_PER_SERVER]],
                "tools_known": len(discovered) if name in surface.servers else None,
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
        rendered = ", ".join(str(tool["name"]) for tool in tools) if tools else "no tools" if known == 0 else "not discovered"
        suffix = f" (+{known - len(tools)} more)" if isinstance(known, int) and known > len(tools) else ""
        detail = f" error={server['error']}" if server["error"] else ""
        lines.append(f"- {server['name']} [{server['status']}]: {rendered}{suffix}{detail}")
    return "\n".join(lines)
