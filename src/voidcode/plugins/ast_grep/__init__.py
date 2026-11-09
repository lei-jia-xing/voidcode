"""The official ``ast_grep`` plugin: structural search and rewrite through ast-grep.

It is the package entry point: a package spec keeps the plugin-local modules importable.

The tool shells out to the ``ast-grep`` CLI, so it declares no import-time
dependency of its own; a missing binary is reported as a tool error.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .ast_grep_tool import AstGrepReplaceTool, AstGrepTool

__all__ = ["AstGrepReplaceTool", "AstGrepTool", "create_plugin"]


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(tools=(AstGrepTool(), AstGrepReplaceTool()))
