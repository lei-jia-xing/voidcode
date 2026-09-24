from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .config import CopilotProviderConfig, ProviderEndpointConfig
from .model_routing import ModelRoute, RoutedTurnProvider, WireRouting
from .openai_native import OpenAIChatCompletionsProvider
from .protocol import StreamableTurnProvider, TurnProvider
from .provider_config import copilot_provider_config
from .provider_table import PROVIDER_TABLE_BY_ID

# Copilot is one host speaking one wire we implement: chat-completions. Its
# catalog rows still carry the upstream wire (``anthropic-messages`` for
# ``claude-*``, ``openai-responses`` for ``gpt-5*``/``grok-4.5``/``4.6``/
# ``oswe*``/``mai-*``), but the provider table's ``wire_source`` is ``provider``,
# so dispatch stays on our implemented wire. OMP's copilot Anthropic route needs a
# parsed credential envelope, ``Authorization: Bearer``, the Copilot identity
# headers and a negotiated integration id (pi ``anthropic.ts:2017-2041,3423``);
# none of that is verifiable without a live Copilot token.
_COPILOT_API_ROUTES: Mapping[str, ModelRoute] = {
    "openai-completions": ModelRoute(wire="openai-chat-completions"),
}
_COPILOT_WIRE_ROUTING = WireRouting(
    provider="github-copilot",
    api_to_route=_COPILOT_API_ROUTES,
    default_api=PROVIDER_TABLE_BY_ID["github-copilot"].wire,
    wire_source=PROVIDER_TABLE_BY_ID["github-copilot"].wire_source,
)


@dataclass(frozen=True, slots=True)
class GithubCopilotModelProvider:
    name: str = "github-copilot"
    config: CopilotProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return copilot_provider_config(self.config)

    def _wire(self, _model: str, _route: ModelRoute) -> StreamableTurnProvider:
        token = None
        if self.config is not None and self.config.auth is not None:
            token = self.config.auth.token
            if token is None and self.config.auth.token_env_var is not None:
                import os

                token = os.environ.get(self.config.auth.token_env_var)
        adapted_config = ProviderEndpointConfig(
            api_key=token,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
        )
        return OpenAIChatCompletionsProvider(name=self.name, config=adapted_config)

    def turn_provider(self) -> TurnProvider:
        return RoutedTurnProvider(
            name=self.name,
            routing=_COPILOT_WIRE_ROUTING,
            build=self._wire,
        )
