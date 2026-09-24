from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from .anthropic_native import AnthropicMessagesProvider
from .config import (
    AnthropicProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    ProviderEndpointConfig,
)
from .google_native import GoogleGenAIProvider
from .model_routing import IMPLEMENTED_API_ROUTES, ModelRoute, RoutedTurnProvider, WireRouting
from .openai_native import OpenAIChatCompletionsProvider
from .protocol import StreamableTurnProvider, TurnProvider
from .provider_config import vendor_endpoint_config
from .provider_table import PROVIDER_TABLE_BY_ID

_OPENCODE_ZEN = PROVIDER_TABLE_BY_ID["opencode-zen"]

# The Zen gateway routes per conversation: a turn that does not name the
# conversation it belongs to is rejected with HTTP 400 ``MissingSessionID``, on
# every wire. So each wire this adapter builds declares the header, and the
# provider resolves ``{session_id}`` from ``ProviderTurnRequest.session_id`` on
# every request -- Zen's client is cached per wire, so a value fixed at
# construction time would freeze the first conversation's id.
_OPENCODE_EXTRA_REQUEST_HEADERS: dict[str, str] = {
    "x-opencode-session": "{session_id}",
    "x-opencode-client": "voidcode",
}

# OpenCode Zen is one gateway host speaking three wires. The Anthropic SDK
# appends its own ``/v1/messages`` path segment, so the Anthropic route carries
# the host root rather than the ``/v1`` chat-completions prefix; the Google route
# keeps that ``/v1`` prefix, which already is its version segment. This host root
# is NOT the table's ``default_base_url`` (which carries the ``/v1`` prefix), so
# it stays an explicit constant rather than a duplicate.
_ZEN_ANTHROPIC_BASE_URL = "https://opencode.ai/zen"

# The wires Zen serves. Each model's wire comes from its catalog row (generated
# from OMP's route pins and upstream npm hints); this map is only the gateway's
# own wire vocabulary, so a wire it does not serve -- the OpenAI Responses API,
# which VoidCode does not implement -- is absent and fails typed instead of being
# sent to an endpoint that does not serve the model.
_ZEN_API_TO_ROUTE: Mapping[str, ModelRoute] = {
    **IMPLEMENTED_API_ROUTES,
    # The Anthropic SDK appends its own ``/v1/messages`` path segment, so the
    # Anthropic route carries the host root rather than the ``/v1`` prefix.
    "anthropic-messages": ModelRoute(wire="anthropic-messages", base_url=_ZEN_ANTHROPIC_BASE_URL),
}

# 2026-09-16: the three Zen wires (chat-completions, Anthropic Messages, Google
# generative-ai) are verified only against mocked HTTP transports: the current Zen
# account returns HTTP 401 ``CreditsError: Insufficient request balance`` for any
# Zen request. The per-conversation headers were accepted, so the failure is
# account balance, not ``MissingSessionID``. Re-check with a funded key: route one
# turn per wire for a Zen model through ``turn_provider()`` (``glm-5.1`` chat,
# ``claude-opus-5`` Anthropic, ``gemini-3-flash`` Google) and expect HTTP 200 with
# ``x-opencode-session`` present.
_ZEN_WIRE_ROUTING = WireRouting(
    provider="opencode-zen",
    api_to_route=_ZEN_API_TO_ROUTE,
    default_api=PROVIDER_TABLE_BY_ID["opencode-zen"].wire,
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

    def _wire(self, _model: str, route: ModelRoute) -> StreamableTurnProvider:
        endpoint = self.provider_config()
        if route.wire == "anthropic-messages":
            return AnthropicMessagesProvider(
                name=self.name,
                # The gateway credential is the OpenCode key on every wire; the
                # Anthropic SDK carries it as ``x-api-key``. Falling back to
                # api.anthropic.com (or its ambient key) is never allowed here,
                # so the route base URL stays authoritative.
                config=AnthropicProviderConfig(
                    api_key=endpoint.api_key,
                    base_url=route.base_url or _ZEN_ANTHROPIC_BASE_URL,
                    timeout_seconds=endpoint.timeout_seconds,
                ),
                extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
            )
        if route.wire == "google-generative-ai":
            return GoogleGenAIProvider(
                name=self.name,
                # Same gateway, same key: only the request shape differs, so an
                # unset endpoint would send the Zen key to Google's own host.
                config=GoogleProviderConfig(
                    auth=GoogleProviderAuthConfig(method="api_key", api_key=endpoint.api_key),
                    base_url=route.base_url or endpoint.base_url,
                ),
                extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
            )
        if route.base_url is not None:
            # A chat route may pin its own base URL; the provider's configured
            # endpoint stays the default.
            endpoint = replace(endpoint, base_url=route.base_url)
        return OpenAIChatCompletionsProvider(
            name=self.name,
            config=endpoint,
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
