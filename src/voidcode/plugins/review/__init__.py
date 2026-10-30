"""The ``review`` command: governed child review of workspace Git changes."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .review_plugin import ReviewCommand


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    unknown = sorted(set(config or {}) - {"providers"})
    if unknown:
        raise ValueError(f"review config does not support field: {unknown[0]}")
    return Plugin(commands=(ReviewCommand(),))
