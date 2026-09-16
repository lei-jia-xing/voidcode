from __future__ import annotations

from dataclasses import dataclass

from .anthropic_native import AnthropicMessagesProvider
from .config import AnthropicProviderConfig, ProviderEndpointConfig
from .protocol import TurnProvider
from .provider_config import anthropic_provider_config


@dataclass(frozen=True, slots=True)
class AnthropicModelProvider:
    name: str = "anthropic"
    config: AnthropicProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return anthropic_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        return AnthropicMessagesProvider(name=self.name, config=self.config)
