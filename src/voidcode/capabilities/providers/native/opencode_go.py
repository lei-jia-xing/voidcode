from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from voidcode.sdk.provider import StreamableTurnProvider, TurnProvider

from .anthropic_native import AnthropicMessagesProvider
from .config import (
    AnthropicProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderEndpointConfig,
    RoutedWire,
    openai_compatible_endpoint_config,
)
from .model_routing import RoutedTurnProvider, WireRouting
from .openai_native import OpenAIChatCompletionsProvider
from .provider_table import PROVIDER_TABLE_BY_ID

# These gateways reject the OpenAI ``tool`` role, so completed tool results are
# replayed as a synthetic user message instead.
_SYNTHETIC_TOOL_FEEDBACK_MODELS = frozenset({"qwen3.6-plus"})

_OPENCODE_GO = PROVIDER_TABLE_BY_ID["opencode-go"]

# Resolve the session header per request; cached clients serve multiple sessions.
_OPENCODE_EXTRA_REQUEST_HEADERS: dict[str, str] = {
    "x-opencode-session": "{session_id}",
    "x-opencode-client": "voidcode",
    "User-Agent": "voidcode",
}

# Unsupported upstream protocols fail before client construction.
_GO_API_TO_ROUTE: Mapping[str, RoutedWire] = {
    "openai-completions": "openai-chat-completions",
    "anthropic-messages": "anthropic-messages",
}
_GO_WIRE_ROUTING = WireRouting(
    provider="opencode-go",
    api_to_route=_GO_API_TO_ROUTE,
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
        if route == "anthropic-messages":
            return AnthropicMessagesProvider(
                name=self.name,
                config=AnthropicProviderConfig(
                    api_key=endpoint.api_key,
                    base_url=endpoint.base_url,
                    timeout_seconds=endpoint.timeout_seconds,
                ),
                extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
            )
        return OpenAIChatCompletionsProvider(
            name=self.name,
            config=replace(endpoint, model_map={}),
            synthetic_tool_feedback_models=_SYNTHETIC_TOOL_FEEDBACK_MODELS,
            extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
        )

    def turn_provider(self) -> TurnProvider:
        endpoint = self.provider_config()
        return RoutedTurnProvider(
            name=self.name,
            routing=_GO_WIRE_ROUTING,
            build=self._wire,
            model_map=endpoint.model_map,
        )
