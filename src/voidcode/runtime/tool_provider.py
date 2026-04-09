from __future__ import annotations

from typing import Protocol

from ..tools import (
    EditTool,
    GlobTool,
    GrepTool,
    ListTool,
    ReadFileTool,
    ShellExecTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from ..tools.contracts import Tool

# Import optional tools that may not exist
_has_optional_tools = False
try:
    from ..tools.apply_patch import ApplyPatchTool as _ApplyPatchTool
    from ..tools.code_search import CodeSearchTool as _CodeSearchTool
    from ..tools.lsp import LspTool as _LspTool
    from ..tools.multi_edit import MultiEditTool as _MultiEditTool
    from ..tools.todo_write import TodoWriteTool as _TodoWriteTool

    _has_optional_tools = True
except ImportError:
    _ApplyPatchTool = None
    _CodeSearchTool = None
    _LspTool = None
    _MultiEditTool = None
    _TodoWriteTool = None


class ToolProvider(Protocol):
    def provide_tools(self) -> tuple[Tool, ...]: ...


class BuiltinToolProvider:
    def provide_tools(self) -> tuple[Tool, ...]:
        tools: list[Tool] = [
            EditTool(),
            GlobTool(),
            GrepTool(),
            ListTool(),
            ReadFileTool(),
            ShellExecTool(),
            WebFetchTool(),
            WebSearchTool(),
            WriteFileTool(),
        ]

        # Add optional tools if available
        if _has_optional_tools:
            if _ApplyPatchTool:
                tools.append(_ApplyPatchTool())
            if _CodeSearchTool:
                tools.append(_CodeSearchTool())
            if _LspTool:
                tools.append(_LspTool())
            if _MultiEditTool:
                tools.append(_MultiEditTool())
            if _TodoWriteTool:
                tools.append(_TodoWriteTool())

        return tuple(tools)
