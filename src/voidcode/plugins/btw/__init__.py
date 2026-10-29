"""The ``btw`` command: answer one side question from the conversation window."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .btw_plugin import BtwCommand


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(BtwCommand(),))
