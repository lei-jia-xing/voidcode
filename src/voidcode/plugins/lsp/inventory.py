"""LSP inventory: the ``lsp`` command listing configured servers and their state.

The inventory is passive: it reads the manager's per-workspace configuration and
runtime server states, so listing servers never spawns one.
"""

from __future__ import annotations

from typing import ClassVar, TypedDict

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.services import PluginServices

from .lsp_manager import LspManager
from .lsp_types import LspManagerMode, LspServerStatus


class _ServerRow(TypedDict):
    name: str
    status: LspServerStatus
    command: list[str]
    extensions: list[str]
    languages: list[str]
    last_error: str | None


class LspInventoryCommand:
    """Report the language servers one workspace resolves to and their state."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="lsp",
        description="List the language servers this workspace resolves to and their state.",
        argument_schema={"type": "object", "properties": {"server": {"type": "string", "minLength": 1}}, "additionalProperties": False},
        text_argument="server",
    )

    def __init__(self, manager: LspManager) -> None:
        self._manager = manager

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        workspace = invocation.context.require_workspace()
        state = self._manager.current_state()
        configuration = self._manager.configuration_for(workspace)
        servers: list[_ServerRow] = [
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
        selected = invocation.arguments.get("server")
        if selected is not None:
            server = next((row for row in servers if row["name"] == selected), None)
            if server is None:
                return CommandResult(status="error", error=f"Unknown LSP server: {selected}. Reopen /lsp to choose an available server.")
            rows = [
                {"label": label, "value": str(value)[:1500]}
                for label, value in (
                    ("Server", server["name"]),
                    ("Status", server["status"]),
                    ("Support enabled", configuration.configured_enabled),
                    ("Mode", state.mode),
                    ("Extensions", ", ".join(server["extensions"]) or "No extension filter"),
                    ("Languages", ", ".join(server["languages"]) or "Not specified"),
                    ("Last error", server["last_error"] or "None reported"),
                )
            ]
            return CommandResult(
                status="ok",
                output=_render_servers([server], mode=state.mode, enabled=configuration.configured_enabled),
                data={"server": server},
                presentation=CommandPresentation(title="Language server details", view_type="fields", data={"rows": rows}),
            )
        if not configuration.configured_enabled or not servers:
            message = _render_servers(servers, mode=state.mode, enabled=configuration.configured_enabled)
            presentation = CommandPresentation(
                title="Language servers",
                view_type="notice",
                data={"message": message + " Check LSP configuration in voidcode.json, then run /core reload."},
            )
        else:
            presentation = CommandPresentation(
                title="Language servers",
                view_type="choices",
                data={
                    "options": [{"id": row["name"], "label": f"{row['name'][:160]} · {row['status']}"} for row in servers[:32]],
                    "action": "lsp",
                    "argument_name": "server",
                },
            )
        return CommandResult(
            status="ok",
            output=_render_servers(servers, mode=state.mode, enabled=configuration.configured_enabled),
            data={"mode": state.mode, "enabled": configuration.configured_enabled, "servers": servers},
            presentation=presentation,
        )


def _render_servers(servers: list[_ServerRow], *, mode: LspManagerMode, enabled: bool) -> str:
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
