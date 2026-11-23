from __future__ import annotations

from dataclasses import dataclass, replace

from voidcode.sdk.provider import StreamableTurnProvider, TurnProvider

from ..registry import PROVIDER_TABLE_BY_ID
from .config import (
    OpenAICompatibleProviderConfig,
    ProviderEndpointConfig,
    RoutedWire,
    openai_compatible_endpoint_config,
)
from .model_routing import IMPLEMENTED_API_ROUTES, RoutedTurnProvider, WireRouting
from .opencode import OpenCodeZenModelProvider

_OPENCODE_GO = PROVIDER_TABLE_BY_ID["opencode-go"]

_GO_WIRE_ROUTING = WireRouting(
    provider="opencode-go",
    api_to_route=IMPLEMENTED_API_ROUTES,
    default_api=_OPENCODE_GO.wire,
)


@dataclass(frozen=True, slots=True)
class OpenCodeGoModelProvider:
    """OpenCode Go gateway with catalog-selected request protocols."""

    name: str = "opencode-go"
    config: OpenAICompatibleProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return openai_compatible_endpoint_config(self.name, self.config)

    def _wire(self, route: RoutedWire) -> StreamableTurnProvider:
        endpoint = self.provider_config()
        return OpenCodeZenModelProvider(
            name=self.name,
            config=replace(endpoint, base_url=self.config.base_url if self.config else None),
        )._wire(route)

    def turn_provider(self) -> TurnProvider:
        endpoint = self.provider_config()
        return RoutedTurnProvider(
            name=self.name,
            routing=_GO_WIRE_ROUTING,
            build=self._wire,
            model_map=endpoint.model_map,
        )
