"""MCP plugin: one ``mcp`` tool backed by the plugin-owned MCP manager.

The entry module assembles the tool from the plugin manifest, so the declared
schema/effects and the registered tool can never drift. It is the package entry
point: a package spec keeps the plugin-local modules importable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, command_definition, tool_definition
from voidcode.sdk.services import PluginServices

from .inventory import McpInventoryCommand, context_contribution
from .mcp_manager import build_mcp_manager
from .mcp_tool import McpTool
from .types import McpConfig, McpServerConfig, McpTransport

_MCP_TRANSPORTS = ("stdio", "remote-http")


def _server_config(raw: object) -> McpServerConfig | None:
    """Parse one server entry; an unusable entry is dropped rather than guessed."""
    if not isinstance(raw, Mapping):
        return None
    transport = raw.get("transport") or "stdio"
    if transport not in _MCP_TRANSPORTS:
        return None
    raw_command = raw.get("command")
    command = tuple(item for item in raw_command if isinstance(item, str)) if isinstance(raw_command, (list, tuple)) else ()
    raw_env = raw.get("env")
    env = {key: value for key, value in raw_env.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(raw_env, Mapping) else {}
    url = raw.get("url")
    return McpServerConfig(
        transport=cast(McpTransport, transport),
        command=command,
        env=env,
        url=url if isinstance(url, str) else None,
    )


def _mcp_config(config: Mapping[str, object] | None) -> McpConfig | None:
    """Parse the plugin's ``mcp`` config section into the local config records."""
    if config is None:
        return None
    enabled = config.get("enabled")
    raw_servers = config.get("servers")
    servers: dict[str, McpServerConfig] | None = None
    if isinstance(raw_servers, Mapping):
        parsed: dict[str, McpServerConfig] = {}
        for name, raw_server in raw_servers.items():
            if not isinstance(name, str) or not name:
                continue
            server = _server_config(raw_server)
            if server is not None:
                parsed[name] = server
        servers = parsed
    timeout = config.get("request_timeout_seconds")
    return McpConfig(
        enabled=enabled if isinstance(enabled, bool) else None,
        servers=servers,
        request_timeout_seconds=float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None,
    )


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    manifest_path = Path(__file__).parent / "voidcode-plugin.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = parse_plugin_manifest(raw_manifest)
    tool_defs = {decl.name: tool_definition(decl) for decl in manifest.tools}
    command_defs = {decl.name: command_definition(decl) for decl in manifest.commands}
    manager = build_mcp_manager(_mcp_config(config))

    def contribute(context: ToolContext, services: PluginServices) -> ContextContribution | None:
        _ = services
        return context_contribution(manager, context)

    def close() -> None:
        manager.shutdown()

    return Plugin(
        tools=(McpTool(definition=tool_defs["mcp"], manager=manager),),
        commands=(McpInventoryCommand(definition=command_defs["mcp"], manager=manager),),
        context=contribute,
        close=close,
    )
