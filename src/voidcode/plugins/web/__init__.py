"""The official ``web`` plugin: outbound fetching and multi-backend web search.

The entry module builds both tools from the plugin manifest, so the declared
name/description/schema/effects and the registered handlers cannot drift.
Backend API keys, the provider order and the exclusion set come from the
process environment, so the plugin keeps no state of its own.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition

from .web_fetch import WebFetchTool
from .web_search import WebSearchTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    manifest = parse_plugin_manifest(json.loads((Path(__file__).parent / "voidcode-plugin.json").read_text(encoding="utf-8")))
    definitions = {decl.name: tool_definition(decl) for decl in manifest.tools}
    return Plugin(
        tools=(
            WebFetchTool(definitions["web_fetch"]),
            WebSearchTool(definitions["web_search"]),
        )
    )
