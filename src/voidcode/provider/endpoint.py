from __future__ import annotations

from dataclasses import dataclass

from .config import ProviderEndpointConfig
from .openai_native import OpenAIChatCompletionsProvider
from .protocol import TurnProvider
from .provider_config import endpoint_provider_config


@dataclass(frozen=True, slots=True)
class OpenAIEndpointProvider:
    name: str = "endpoint"
    config: ProviderEndpointConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return endpoint_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        return OpenAIChatCompletionsProvider(name=self.name, config=self.provider_config())


__all__ = ["OpenAIEndpointProvider"]
