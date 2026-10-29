"""The ``review`` command: governed child review of workspace Git changes."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .review_plugin import ReviewCommand


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(ReviewCommand(),))
