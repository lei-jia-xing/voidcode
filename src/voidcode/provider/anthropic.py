from __future__ import annotations

from dataclasses import dataclass

from .anthropic_native import AnthropicMessagesProvider
from .config import AnthropicProviderConfig
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class AnthropicModelProvider:
    name: str = "anthropic"
    config: AnthropicProviderConfig | None = None

    def provider_config(self) -> AnthropicProviderConfig | None:
        return self.config

    def turn_provider(self) -> TurnProvider:
        return AnthropicMessagesProvider(name=self.name, config=self.config)
