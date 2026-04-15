from __future__ import annotations

from typing import Protocol

from ..tools.contracts import Tool
from ..tools.edit import EditTool
from ..tools.glob import GlobTool
from ..tools.grep import GrepTool
from ..tools.list_dir import ListTool
from ..tools.read_file import ReadFileTool
from ..tools.shell_exec import ShellExecTool
from ..tools.web_fetch import WebFetchTool
from ..tools.web_search import WebSearchTool
from ..tools.write_file import WriteFileTool

# Import optional tools independently so one failure doesn't hide others.
try:
    from ..tools.apply_patch import ApplyPatchTool as _ApplyPatchTool
except ImportError:
    _ApplyPatchTool = None

try:
    from ..tools.ast_grep import AstGrepReplaceTool as _AstGrepReplaceTool
    from ..tools.ast_grep import AstGrepSearchTool as _AstGrepSearchTool
except ImportError:
    _AstGrepReplaceTool = None
    _AstGrepSearchTool = None

try:
    from ..tools.code_search import CodeSearchTool as _CodeSearchTool
except ImportError:
    _CodeSearchTool = None

try:
    from ..tools.multi_edit import MultiEditTool as _MultiEditTool
except ImportError:
    _MultiEditTool = None

try:
    from ..tools.todo_write import TodoWriteTool as _TodoWriteTool
except ImportError:
    _TodoWriteTool = None


class ToolProvider(Protocol):
    def provide_tools(self) -> tuple[Tool, ...]: ...


class BuiltinToolProvider:
    def __init__(self, *, lsp_tool: Tool | None = None, mcp_tools: tuple[Tool, ...] = ()) -> None:
        self._lsp_tool = lsp_tool
        self._mcp_tools = mcp_tools

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

        if self._lsp_tool is not None:
            tools.append(self._lsp_tool)

        tools.extend(self._mcp_tools)

        # Add optional tools if available.
        if _ApplyPatchTool is not None:
            tools.append(_ApplyPatchTool())
        if _AstGrepSearchTool is not None:
            tools.append(_AstGrepSearchTool())
        if _AstGrepReplaceTool is not None:
            tools.append(_AstGrepReplaceTool())
        if _CodeSearchTool is not None:
            tools.append(_CodeSearchTool())
        if _MultiEditTool is not None:
            tools.append(_MultiEditTool())
        if _TodoWriteTool is not None:
            tools.append(_TodoWriteTool())

        return tuple(tools)
