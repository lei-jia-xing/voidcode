"""The official ``web`` plugin: Exa-backed web search.

The Exa API key comes from the process environment, so the plugin keeps no
state of its own.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .web_search import WebSearchTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(tools=(WebSearchTool(),))
