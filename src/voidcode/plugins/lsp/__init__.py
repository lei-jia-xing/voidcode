"""The official ``lsp`` plugin: one language-server tool plus one document query.

Owns the LSP connection manager (session-scoped servers over the host's
supervised service processes, document state, request timeouts, shutdown) and
the ``lsp`` tool that renders bounded pages of 13 language-server operations.
Servers are resolved from the plugin config, or derived from workspace markers
when the config declares none, and are spawned lazily on the first request.
The ``lsp`` command reports the same manager's servers without spawning one,
and the ``workspace.document_version`` query answers the caller's session's
already-opened documents without starting a server or sending didOpen/didChange.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, PluginQuery, PluginQueryCaller, command_definition, tool_definition

from .inventory import LspInventoryCommand
from .lsp_manager import LspManager, build_lsp_manager
from .lsp_tool import LspTool
from .lsp_types import lsp_config_from_mapping

DOCUMENT_VERSION_QUERY = "workspace.document_version"


def _document_version_query(manager: LspManager) -> PluginQuery:
    def handler(arguments: Mapping[str, object], caller: PluginQueryCaller) -> Mapping[str, object] | None:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("workspace.document_version requires a string 'path' argument")
        requested = Path(raw_path)
        candidate = (requested if requested.is_absolute() else caller.workspace / requested).resolve()
        state = manager.document_state(session_id=caller.session_id, file_path=candidate, workspace=caller.workspace)
        if state is None:
            return None
        return {
            "path": candidate.as_posix(),
            "version": state.version,
            "content_hash": state.content_hash,
            "position_encoding": state.position_encoding,
        }

    return PluginQuery(name=DOCUMENT_VERSION_QUERY, handler=handler)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    manifest = parse_plugin_manifest(json.loads((Path(__file__).parent / "voidcode-plugin.json").read_text(encoding="utf-8")))
    definitions = {decl.name: tool_definition(decl) for decl in manifest.tools}
    commands = {decl.name: command_definition(decl) for decl in manifest.commands}
    manager = build_lsp_manager(lsp_config_from_mapping(config))

    def close() -> None:
        manager.shutdown()

    return Plugin(
        tools=(LspTool(definitions["lsp"], manager),),
        commands=(LspInventoryCommand(commands["lsp"], manager),),
        queries=(_document_version_query(manager),),
        close=close,
    )
