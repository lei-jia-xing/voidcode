from __future__ import annotations

from dataclasses import dataclass, replace

from .anthropic_native import AnthropicMessagesProvider
from .config import (
    AnthropicProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    ProviderEndpointConfig,
)
from .google_native import GoogleGenAIProvider
from .model_routing import ModelRoute, RoutedTurnProvider, WireRouting
from .openai_native import OpenAIChatCompletionsProvider
from .protocol import TurnProvider

_OPENCODE_ZEN_BASE_URL = "https://opencode.ai/zen/v1"
_OPENCODE_ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
_OPENCODE_API_KEY_ENV_VAR = "OPENCODE_API_KEY"

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
# keeps that ``/v1`` prefix, which already is its version segment.
_ZEN_ANTHROPIC_BASE_URL = "https://opencode.ai/zen"

# The wires Zen serves the models VoidCode ships over, per OMP's Zen catalog
# (pi-ai ``opencode.json``): everything else -- including the models OMP does not
# list at all -- uses the default chat-completions route, so ``model_map`` aliases
# and user-mounted gateways keep working.
_ZEN_ANTHROPIC_MODELS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-haiku-4-5",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-4",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "qwen3.5-plus",
    "qwen3.6-plus",
)
_ZEN_GOOGLE_MODELS: tuple[str, ...] = (
    "gemini-3-flash",
    "gemini-3.1-pro",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
)
# Upstream serves these over the OpenAI Responses API, which VoidCode does not
# implement. Sending them down chat-completions would target an endpoint that
# does not serve them, so they fail typed instead.
_ZEN_RESPONSES_MODELS: tuple[str, ...] = (
    "gpt-5",
    "gpt-5-codex",
    "gpt-5-nano",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.4-pro",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "grok-4.5",
    "grok-4.6",
    "grok-build-0.1",
    "muse-spark-1.2",
)

# 2026-09-16: the three Zen wires (chat-completions, Anthropic Messages, Google
# generative-ai) are verified only against mocked HTTP transports: the current Zen
# account returns HTTP 401 ``CreditsError: Insufficient request balance`` for any
# Zen request. The per-conversation headers were accepted, so the failure is
# account balance, not ``MissingSessionID``. Re-check with a funded key: route one
# turn per wire for a Zen model through ``turn_provider()`` (``glm-5.1`` chat,
# ``claude-opus-5`` Anthropic, ``gemini-3-flash`` Google) and expect HTTP 200 with
# ``x-opencode-session`` present.
_ZEN_ROUTING = WireRouting(
    default=ModelRoute(wire="openai-chat-completions"),
    overrides={
        **dict.fromkeys(_ZEN_ANTHROPIC_MODELS, ModelRoute(wire="anthropic-messages", base_url=_ZEN_ANTHROPIC_BASE_URL)),
        # The Google route pins no base URL: the gateway's own endpoint already is
        # the Google surface's version root, so ``providers.opencode.base_url``
        # stays authoritative there exactly as it does for the default route.
        **dict.fromkeys(_ZEN_GOOGLE_MODELS, ModelRoute(wire="google-generative-ai")),
        **dict.fromkeys(_ZEN_RESPONSES_MODELS, None),
    },
)


@dataclass(frozen=True, slots=True)
class OpenCodeModelProvider:
    name: str = "opencode"
    config: ProviderEndpointConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        if self.config is None:
            return ProviderEndpointConfig(
                base_url=_OPENCODE_ZEN_BASE_URL,
                discovery_base_url=_OPENCODE_ZEN_MODELS_URL,
                api_key_env_var=_OPENCODE_API_KEY_ENV_VAR,
                model_map={},
            )
        discovery_base_url = self.config.discovery_base_url
        if discovery_base_url is None:
            discovery_base_url = None if self.config.base_url else _OPENCODE_ZEN_MODELS_URL

        return ProviderEndpointConfig(
            api_key=self.config.api_key,
            api_key_env_var=self.config.api_key_env_var,
            base_url=self.config.base_url or _OPENCODE_ZEN_BASE_URL,
            discovery_base_url=discovery_base_url,
            auth_header=self.config.auth_header,
            auth_scheme=self.config.auth_scheme,
            auth_scheme_explicit=self.config.auth_scheme_explicit,
            ssl_verify=self.config.ssl_verify,
            timeout_seconds=self.config.timeout_seconds,
            model_map=(dict(self.config.model_map) if self.config.model_map else {}),
            transient_retry=self.config.transient_retry,
        )

    def _wire(self, _model: str, route: ModelRoute) -> TurnProvider:
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
            routing=_ZEN_ROUTING,
            build=self._wire,
            model_map=endpoint.model_map,
        )
