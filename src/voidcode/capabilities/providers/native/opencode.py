from __future__ import annotations

from dataclasses import dataclass, replace

from voidcode.sdk.provider import StreamableTurnProvider, TurnProvider

from .anthropic_native import AnthropicMessagesProvider
from .config import (
    AnthropicProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    ProviderEndpointConfig,
    RoutedWire,
)
from .google_native import GoogleGenAIProvider
from .model_routing import IMPLEMENTED_API_ROUTES, RoutedTurnProvider, WireRouting
from .openai_native import OpenAIChatCompletionsProvider
from .provider_config import vendor_endpoint_config
from .provider_table import PROVIDER_TABLE_BY_ID

_OPENCODE_ZEN = PROVIDER_TABLE_BY_ID["opencode-zen"]

# Resolve the session header per request; cached clients serve multiple sessions.
_OPENCODE_EXTRA_REQUEST_HEADERS: dict[str, str] = {
    "x-opencode-session": "{session_id}",
    "x-opencode-client": "voidcode",
    "User-Agent": "voidcode",
}

_ZEN_WIRE_ROUTING = WireRouting(
    provider="opencode-zen",
    api_to_route=IMPLEMENTED_API_ROUTES,
    default_api=_OPENCODE_ZEN.wire,
)


@dataclass(frozen=True, slots=True)
class OpenCodeZenModelProvider:
    name: str = "opencode-zen"
    config: ProviderEndpointConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return vendor_endpoint_config(
            self.config,
            base_url=_OPENCODE_ZEN.default_base_url,
            api_key_env_var=_OPENCODE_ZEN.env_vars[0],
        )

    def _wire(self, route: RoutedWire) -> StreamableTurnProvider:
        endpoint = self.provider_config()
        vendor_default = self.config is None or self.config.base_url is None
        if vendor_default:
            endpoint = replace(
                endpoint,
                base_url={
                    "openai-chat-completions": _OPENCODE_ZEN.default_base_url,
                    "anthropic-messages": "https://opencode.ai/inference/anthropic",
                    "google-generative-ai": "https://opencode.ai/inference/google/v1beta",
                }[route],
            )
        if route == "anthropic-messages":
            return AnthropicMessagesProvider(
                name=self.name,
                auth_header="Authorization" if vendor_default else None,
                config=AnthropicProviderConfig(
                    api_key=endpoint.api_key,
                    base_url=endpoint.base_url,
                    timeout_seconds=endpoint.timeout_seconds,
                ),
                extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
            )
        if route == "google-generative-ai":
            return GoogleGenAIProvider(
                name=self.name,
                auth_header="Authorization" if vendor_default else None,
                # Same gateway, same key: only the request shape differs, so an
                # unset endpoint would send the Zen key to Google's own host.
                config=GoogleProviderConfig(
                    auth=GoogleProviderAuthConfig(method="api_key", api_key=endpoint.api_key),
                    base_url=endpoint.base_url,
                    timeout_seconds=endpoint.timeout_seconds,
                ),
                extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
            )
        return OpenAIChatCompletionsProvider(
            name=self.name,
            config=replace(endpoint, model_map={}),
            extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
        )

    def turn_provider(self) -> TurnProvider:
        endpoint = self.provider_config()
        return RoutedTurnProvider(
            name=self.name,
            routing=_ZEN_WIRE_ROUTING,
            build=self._wire,
            model_map=endpoint.model_map,
        )
