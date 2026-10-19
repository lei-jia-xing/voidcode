"""Files plugin: workspace read, write, edit, patch and search tools.

The entry module assembles the tools from the plugin manifest, so the declared
schema/effects and the registered tools can never drift. It is the package
entry point: a package spec keeps the plugin-local modules importable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition

from .apply_workspace_edit import ApplyWorkspaceEditTool
from .edit_tool import EditTool
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .read_tool import ReadTool
from .write_tool import WriteTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    manifest_path = Path(__file__).parent / "voidcode-plugin.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = parse_plugin_manifest(raw_manifest)
    tool_defs = {decl.name: tool_definition(decl) for decl in manifest.tools}
    return Plugin(
        tools=(
            ReadTool(definition=tool_defs["read"]),
            WriteTool(definition=tool_defs["write"]),
            EditTool(definition=tool_defs["edit"]),
            ApplyWorkspaceEditTool(definition=tool_defs["apply_workspace_edit"]),
            GlobTool(tool_defs["glob"]),
            GrepTool(tool_defs["grep"]),
        ),
        providers=(),
        close=None,
    )
