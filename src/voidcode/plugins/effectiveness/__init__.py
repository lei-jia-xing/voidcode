"""The ``effectiveness`` command: aggregate session tool-outcome metrics."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .effectiveness_plugin import EffectivenessCommand


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(EffectivenessCommand(),))
