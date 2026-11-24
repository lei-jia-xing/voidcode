"""Optional files adapter over the shared product capabilities."""

from collections.abc import Mapping

from voidcode.capabilities.files import create_tools
from voidcode.capabilities.files.find_queries import create_find_queries
from voidcode.capabilities.files.resource_readers import create_resource_readers
from voidcode.sdk.plugin import Plugin


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    del config
    return Plugin(tools=create_tools(), queries=create_find_queries(), resource_readers=create_resource_readers())
