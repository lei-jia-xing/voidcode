"""Session information plugin: read-only status/context/usage/export/copy panels.

The panels read the calling session's committed events and measured usage
through the SDK services; they never mutate state and never touch storage.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .panels import SessionInfoCommand


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=tuple(SessionInfoCommand(panel) for panel in ("session_status", "context", "usage", "export", "copy")))
