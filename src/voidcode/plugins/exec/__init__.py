"""Optional exec adapter over the shared product capabilities."""

from collections.abc import Mapping

from voidcode.capabilities.exec import create_plugin as _create_capabilities
from voidcode.sdk.plugin import Plugin


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    return _create_capabilities(config)
