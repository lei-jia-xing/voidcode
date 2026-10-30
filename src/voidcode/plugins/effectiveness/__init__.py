"""The ``effectiveness`` command: aggregate session tool-outcome metrics."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .effectiveness_plugin import EffectivenessCommand


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    unknown = sorted(set(config or {}) - {"providers"})
    if unknown:
        raise ValueError(f"effectiveness config does not support field: {unknown[0]}")
    return Plugin(commands=(EffectivenessCommand(),))
