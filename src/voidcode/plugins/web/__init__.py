"""The official ``web`` plugin: search and fetch pages over HTTP(S).

Search runs against the first configured backend; fetch retrieves one page and
renders it to Markdown. Credentials and endpoint overrides come from the
process environment. The plugin keeps no state of its own.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .web_fetch import WebFetchTool
from .web_reader import WEB_READER
from .web_search import WebSearchTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(tools=(WebSearchTool(), WebFetchTool()), resource_readers=(WEB_READER,))
