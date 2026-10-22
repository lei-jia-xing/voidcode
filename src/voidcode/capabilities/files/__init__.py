"""Files plugin: workspace read, write, edit, and search tools (patch runs inside edit)."""

from __future__ import annotations

from typing import Any

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import TextOutput, Tool, ToolCall, ToolDiagnostics, ToolFailure, ToolResult

from .apply_workspace_edit import ApplyWorkspaceEditTool
from .edit_tool import EditTool
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .read_tool import ReadTool
from .repair import ToolDiagnosticError
from .write_tool import WriteTool


class _ModelVisibleTool:
    """Public boundary: a raised file diagnostic becomes the model-visible typed failure.

    The runtime redacts an escaping exception to its class name, so the shared guards' refusals
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


def create_tools() -> tuple[Tool, ...]:
    """Fresh file tools shared by static core defaults and the optional adapter."""
    return tuple(_ModelVisibleTool(tool) for tool in (ReadTool(), WriteTool(), EditTool(), ApplyWorkspaceEditTool(), GlobTool(), GrepTool()))
