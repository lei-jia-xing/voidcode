from __future__ import annotations

from typing import Protocol

from ..tools import GrepTool, ReadFileTool, ShellExecTool, WriteFileTool
from ..tools.contracts import Tool


class ToolProvider(Protocol):
    def provide_tools(self) -> tuple[Tool, ...]: ...


class BuiltinToolProvider:
    def provide_tools(self) -> tuple[Tool, ...]:
        return (GrepTool(), ReadFileTool(), ShellExecTool(), WriteFileTool())
