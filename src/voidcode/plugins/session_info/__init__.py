"""Session information plugin: read-only context/usage/export/copy panels.

The panels read the calling session's committed events and measured usage
through the SDK services; they never mutate state and never touch storage.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .panels import SessionInfoCommand


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    unknown = sorted(set(config or {}) - {"providers"})
    if unknown:
        raise ValueError(f"session_info config does not support field: {unknown[0]}")
    return Plugin(commands=tuple(SessionInfoCommand(panel) for panel in ("context", "usage", "export", "copy")))
