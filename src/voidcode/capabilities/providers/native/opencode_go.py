from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from voidcode.sdk.provider import StreamableTurnProvider, TurnProvider

from .anthropic_native import AnthropicMessagesProvider
from .config import (
    AnthropicProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderEndpointConfig,
    openai_compatible_endpoint_config,
)
from .model_catalog import ToolFeedbackMode
from .model_routing import ModelRoute, RoutedTurnProvider, WireRouting
from .openai_native import OpenAIChatCompletionsProvider
from .provider_table import PROVIDER_TABLE_BY_ID

# These gateways reject the OpenAI ``tool`` role, so completed tool results are
# replayed as a synthetic user message instead.
_TOOL_FEEDBACK_OVERRIDES: dict[str, ToolFeedbackMode] = {
    "qwen3.6-plus": "synthetic_user_message",
}

# OpenCode Go is one gateway host speaking two wires: the Anthropic SDK appends
# its own ``/v1/messages`` path segment, so the Anthropic route carries the host
# root rather than the ``/v1`` chat-completions prefix. That root IS the table's
# ``default_base_url``, so the constant is read from the table rather than restated.
_OPENCODE_GO = PROVIDER_TABLE_BY_ID["opencode-go"]

# The Go gateway routes per conversation: a turn that does not name the
# conversation it belongs to is rejected with HTTP 400 ``MissingSessionID``, on
# every wire. So each wire this adapter builds declares the header, and the
# provider resolves ``{session_id}`` from ``ProviderTurnRequest.session_id`` on
# every request -- the client is cached per wire, so a value fixed at
# construction time would freeze the first conversation's id.
_OPENCODE_EXTRA_REQUEST_HEADERS: dict[str, str] = {
    "x-opencode-session": "{session_id}",
    "x-opencode-client": "voidcode",
}

# The wires the Go gateway serves. Each model's wire comes from its catalog row
# (generated from upstream protocol metadata); this map is only the
# gateway's own wire vocabulary, so a wire it does not serve -- the OpenAI
# Responses API, which VoidCode does not implement -- is absent and fails typed
# instead of being sent to an endpoint that does not serve the model.
_GO_API_TO_ROUTE: Mapping[str, ModelRoute] = {
    "openai-completions": ModelRoute(wire="openai-chat-completions"),
    # The Anthropic SDK appends its own ``/v1/messages`` path segment, so the
    # Anthropic route carries the host root rather than the ``/v1`` prefix.
    "anthropic-messages": ModelRoute(wire="anthropic-messages", base_url=_OPENCODE_GO.default_base_url),
}
_GO_WIRE_ROUTING = WireRouting(
    provider="opencode-go",
    api_to_route=_GO_API_TO_ROUTE,
    default_api=PROVIDER_TABLE_BY_ID["opencode-go"].wire,
)


@dataclass(frozen=True, slots=True)
class OpenCodeGoModelProvider:
    """OpenCode Go gateway with catalog-selected request protocols."""

    name: str = "opencode-go"
    config: OpenAICompatibleProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return openai_compatible_endpoint_config(self.name, self.config)

    def _wire(self, _model: str, route: ModelRoute) -> StreamableTurnProvider:
        endpoint = self.provider_config()
        if route.wire == "anthropic-messages":
            return AnthropicMessagesProvider(
                name=self.name,
                # The gateway credential is the OpenCode key on both wires; the
                # Anthropic SDK carries it as ``x-api-key``. Falling back to
                # api.anthropic.com (or its ambient key) is never allowed here,
                # so the route base URL stays authoritative.
                config=AnthropicProviderConfig(
                    api_key=endpoint.api_key,
                    base_url=route.base_url or _OPENCODE_GO.default_base_url,
                    timeout_seconds=endpoint.timeout_seconds,
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
            tool_feedback_model_overrides=_TOOL_FEEDBACK_OVERRIDES,
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
