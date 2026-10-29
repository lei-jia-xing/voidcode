"""The ``!`` command plugin."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .user_shell_plugin import ShellCommand


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(ShellCommand(),))
