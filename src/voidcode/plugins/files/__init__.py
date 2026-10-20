"""Files plugin: workspace read, write, edit, and search tools (patch runs inside edit).

The entry module assembles the tools from the plugin manifest, so the declared
schema/effects and the registered tools can never drift. It is the package
entry point: a package spec keeps the plugin-local modules importable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import TextOutput, ToolCall, ToolDiagnostics, ToolFailure, ToolResult

from .apply_workspace_edit import ApplyWorkspaceEditTool
from .edit_tool import EditTool
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .read_tool import ReadTool
from .repair import ToolDiagnosticError
from .write_tool import WriteTool


class _ModelVisibleTool:
    """Public boundary: a raised file diagnostic becomes the model-visible typed failure.

    The host redacts an escaping exception to its class name, so the shared guards' refusals
    (unseen ranges, write-without-read) must be answered here with their own text.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.definition = inner.definition

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        try:
            return self._inner.invoke(call, context=context, services=services)
        except ToolDiagnosticError as exc:
            return ToolFailure(
                tool_name=call.tool_name,
                error=exc.message,
                output=TextOutput(text=exc.message),
                diagnostics=ToolDiagnostics(
                    kind=exc.error_kind,
                    summary=exc.message,
                    details=exc.error_details,
                    guidance=exc.retry_guidance,
                ),
            )


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    manifest_path = Path(__file__).parent / "voidcode-plugin.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = parse_plugin_manifest(raw_manifest)
    tool_defs = {decl.name: tool_definition(decl) for decl in manifest.tools}
    return Plugin(
        tools=tuple(
            _ModelVisibleTool(tool)
            for tool in (
                ReadTool(definition=tool_defs["read"]),
                WriteTool(definition=tool_defs["write"]),
                EditTool(definition=tool_defs["edit"]),
                ApplyWorkspaceEditTool(definition=tool_defs["apply_workspace_edit"]),
                GlobTool(tool_defs["glob"]),
                GrepTool(tool_defs["grep"]),
            )
        ),
        providers=(),
        close=None,
    )
