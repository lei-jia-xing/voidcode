"""The official ``exec`` plugin: workspace command execution.

Owns the shell execution tool: command interception, process-group lifetime,
bounded capture and the honest partial/settled reporting the runtime relies on.
It also owns background process management on the kernel's process lease:
supervised processes the host owns, whose identity this plugin persists so a
later host can report them honestly instead of guessing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition

from .background_process import BackgroundProcessTool
from .shell_exec import ShellExecTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    manifest = parse_plugin_manifest(json.loads((Path(__file__).parent / "voidcode-plugin.json").read_text(encoding="utf-8")))
    definitions = {decl.name: tool_definition(decl) for decl in manifest.tools}
    return Plugin(
        tools=(
            ShellExecTool(definitions["shell_exec"]),
            BackgroundProcessTool(definitions["background_process"]),
        )
    )
