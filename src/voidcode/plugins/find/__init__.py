"""Behavioral ``find`` plugin: locate code by what it does, not its name."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .find_tool import FindTool

__all__ = ["FindTool", "create_plugin"]


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(tools=(FindTool(),))
