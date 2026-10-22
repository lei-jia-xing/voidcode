"""Optional files adapter over the shared product capabilities."""

from collections.abc import Mapping

from voidcode.capabilities.files import create_tools
from voidcode.sdk.plugin import Plugin


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    del config
    return Plugin(tools=create_tools())
