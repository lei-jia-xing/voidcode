"""The official ``ast_grep`` plugin: structural search and rewrite through ast-grep.

It is the package entry point: a package spec keeps the plugin-local modules importable.

The tools shell out to the ``ast-grep`` CLI, so the plugin declares no
import-time dependency. The executable is discovered on ``PATH``; a missing
binary is a typed ``tool_environment_error`` with install guidance at the
first invocation, not an activation-time failure.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .ast_grep_tool import AstGrepReplaceTool, AstGrepTool

__all__ = ["AstGrepReplaceTool", "AstGrepTool", "create_plugin"]


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(tools=(AstGrepTool(), AstGrepReplaceTool()))
