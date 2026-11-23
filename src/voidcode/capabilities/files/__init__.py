"""Shared workspace read, write, edit and search tools."""

from __future__ import annotations

from voidcode.sdk.tools import Tool

from .edit_tool import EditTool
from .glob_tool import GlobTool
from .grep_tool import GrepTool
from .read_tool import ReadTool
from .write_tool import WriteTool


def create_tools() -> tuple[Tool, ...]:
    """Create the file plugin's tools; each public invoke owns its typed failure boundary."""
    return (ReadTool(), WriteTool(), EditTool(), GlobTool(), GrepTool())
