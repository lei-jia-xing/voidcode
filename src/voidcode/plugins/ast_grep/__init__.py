"""The official ``ast_grep`` plugin: structural search and rewrite through ast-grep.

The entry module assembles the tool from the plugin manifest, so the declared
schema/effects and the registered handler can never drift. It is the package
entry point: a package spec keeps the plugin-local modules importable.

The tool shells out to the ``ast-grep`` CLI, so it declares no import-time
dependency of its own; a missing binary is reported as a tool error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition

from .ast_grep_tool import AstGrepTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    manifest_path = Path(__file__).parent / "voidcode-plugin.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = parse_plugin_manifest(raw_manifest)
    tool_defs = {decl.name: tool_definition(decl) for decl in manifest.tools}
    return Plugin(tools=(AstGrepTool(definition=tool_defs["ast_grep"]),))
