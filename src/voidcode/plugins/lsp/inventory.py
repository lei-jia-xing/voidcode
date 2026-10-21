"""LSP inventory: the ``lsp`` command listing configured servers and their state.

The inventory is passive: it reads the manager's per-workspace configuration and
runtime server states, so listing servers never spawns one.
"""

from __future__ import annotations

from typing import Any, ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.services import PluginServices


class LspInventoryCommand:
    """Report the language servers one workspace resolves to and their state."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="lsp",
        description="List the language servers this workspace resolves to and their state.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        workspace = invocation.context.require_workspace()
        state = self._manager.current_state()
        configuration = self._manager.configuration_for(workspace) if hasattr(self._manager, "configuration_for") else state.configuration
        servers: list[dict[str, Any]] = [
            {
                "name": name,
                "status": state.servers[name].status if name in state.servers else "stopped",
                "command": list(server.command),
                "extensions": list(server.extensions),
                "languages": list(server.languages),
                "last_error": state.servers[name].last_error if name in state.servers else None,
            }
            for name, server in sorted(configuration.servers.items())
        ]
        return CommandResult(
            status="ok",
            output=_render_servers(servers, mode=state.mode, enabled=configuration.configured_enabled),
            data={"mode": state.mode, "enabled": configuration.configured_enabled, "servers": servers},
        )


def _render_servers(servers: list[dict[str, Any]], *, mode: str, enabled: bool) -> str:
    if not enabled:
        return "LSP support is disabled by configuration."
    if not servers:
        return "No language server matches this workspace."
    lines = [f"LSP ({mode}): {len(servers)} server(s) available:"]
    for server in servers:
        extensions = ", ".join(server["extensions"]) or "no extension filter"
        detail = f" [{server['status']}]"
        if server["last_error"]:
            detail += f" last_error={server['last_error']}"
        lines.append(f"- {server['name']}{detail}: {extensions}")
    return "\n".join(lines)
