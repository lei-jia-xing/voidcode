from __future__ import annotations

from dataclasses import dataclass, replace

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
from .protocol import TurnProvider

# These gateways reject the OpenAI ``tool`` role, so completed tool results are
# replayed as a synthetic user message instead.
_TOOL_FEEDBACK_OVERRIDES: dict[str, ToolFeedbackMode] = {
    "qwen3.6-plus": "synthetic_user_message",
}

# OpenCode Go is one gateway host speaking two wires: the Anthropic SDK appends
# its own ``/v1/messages`` path segment, so the Anthropic route carries the host
# root rather than the ``/v1`` chat-completions prefix.
_GO_ANTHROPIC_BASE_URL = "https://opencode.ai/zen/go"

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

# 2026-09-16: the Go gateway returns HTTP 500
# ``{"type":"error","error":{"type":"error","message":"Internal server error"}}``
# for ``minimax-m2.7`` in every request shape tried -- a standard OpenAI ``tool``
# role replay, the synthetic-user-message form, a plain tool-free two-message turn,
# and streaming -- though ``GET https://opencode.ai/zen/go/v1/models`` still lists
# it. The tool-role A/B is therefore inconclusive, so the routing and feedback
# defaults stay OMP-aligned (chat-completions; no ``_TOOL_FEEDBACK_OVERRIDES``
# entry) on the understanding that this is an upstream gateway defect, not a
# VoidCode bug. Re-check once upstream recovers: send the same conversation twice
# for ``opencode-go/minimax-m2.7``, once with the standard ``tool`` role and once
# with ``tool_feedback_model_overrides={"minimax-m2.7": "synthetic_user_message"}``,
# restoring the override only if the standard form fails while the synthetic one
# succeeds.
_GO_ROUTING = WireRouting(
    default=ModelRoute(wire="openai-chat-completions"),
    overrides={
        # MiniMax M3 is served over Anthropic Messages while every other Go
        # model speaks chat-completions at ``…/zen/go/v1``.
        "minimax-m3": ModelRoute(wire="anthropic-messages", base_url=_GO_ANTHROPIC_BASE_URL),
        # Upstream serves ``gpt-5.6-luna`` over the OpenAI Responses API, which
        # VoidCode does not implement. Sending it down chat-completions would
        # target an endpoint that does not serve it, so it fails typed instead.
        "gpt-5.6-luna": None,
    },
)


@dataclass(frozen=True, slots=True)
class OpenCodeGoModelProvider:
    """OpenCode Go Model Provider.

    OpenCode Go provides unified access to multiple Chinese AI models through
    a single subscription at https://opencode.ai

    Supported models: GLM-5.x, Kimi K2.6/K2.7/K3, MiniMax M2.7/M3, MiMo v2.5,
    Qwen3.6+/3.7/3.8, DeepSeek V4, Hy3, Grok 4.5 and GPT-5.6 Luna.

    Usage:
        providers:
          opencode-go:
            api_key: "your-api-key"  # or set OPENCODE_API_KEY env var
            model_map:
              glm-5: glm-5  # optional model alias

    Environment Variables:
        OPENCODE_API_KEY: API key shared by OpenCode Zen and OpenCode Go

    Note:
        Every model is routed through OpenCode Go's gateway at
        https://opencode.ai/zen/go, but not every model speaks the same wire
        there: ``minimax-m3`` is spoken over the Anthropic Messages API
        (https://opencode.ai/zen/go/v1/messages) and the rest over the
        OpenAI-compatible chat-completions API
        (https://opencode.ai/zen/go/v1/chat/completions).
        Model IDs in config use format: opencode-go/<model-id>
    """

    name: str = "opencode-go"
    config: OpenAICompatibleProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig | None:
        return openai_compatible_endpoint_config(self.name, self.config)

    def _wire(self, _model: str, route: ModelRoute) -> TurnProvider:
        endpoint = self.provider_config()
        if route.wire == "anthropic-messages":
            return AnthropicMessagesProvider(
                name=self.name,
                # The gateway credential is the OpenCode key on both wires; the
                # Anthropic SDK carries it as ``x-api-key``. Falling back to
                # api.anthropic.com (or its ambient key) is never allowed here,
                # so the route base URL stays authoritative.
                config=AnthropicProviderConfig(
                    api_key=None if endpoint is None else endpoint.api_key,
                    base_url=route.base_url or _GO_ANTHROPIC_BASE_URL,
                    timeout_seconds=None if endpoint is None else endpoint.timeout_seconds,
                ),
                extra_request_headers=_OPENCODE_EXTRA_REQUEST_HEADERS,
            )
        if route.base_url is not None:
            # A chat route may pin its own base URL; the provider's configured
            # endpoint stays the default.
            endpoint = ProviderEndpointConfig(base_url=route.base_url) if endpoint is None else replace(endpoint, base_url=route.base_url)
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
            routing=_GO_ROUTING,
            build=self._wire,
            model_map={} if endpoint is None else endpoint.model_map,
        )
