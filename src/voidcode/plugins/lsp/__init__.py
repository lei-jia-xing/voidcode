"""The official ``lsp`` plugin: one read-only language-server tool.

Owns the LSP connection manager (workspace-scoped shared servers, document
state, request timeouts, shutdown) and the ``lsp`` tool that renders bounded
pages of 13 language-server operations. Servers are resolved from the plugin
config, or derived from workspace markers when the config declares none, and
are spawned lazily on the first request. The ``lsp`` command reports the same
manager's servers without spawning one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, command_definition, tool_definition

from .inventory import LspInventoryCommand
from .lsp_manager import build_lsp_manager
from .lsp_tool import LspTool
from .lsp_types import lsp_config_from_mapping


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
        close=close,
    )
