"""The official ``web`` plugin: search using the first configured backend.

Credentials and endpoint overrides come from the process environment. Jina is
the final, keyless backend; a selected backend failure is returned directly.
The plugin keeps no state of its own.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .web_search import WebSearchTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(tools=(WebSearchTool(),))
