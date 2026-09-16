from __future__ import annotations

from dataclasses import dataclass

from .config import OpenAIProviderConfig, ProviderEndpointConfig
from .openai_native import OpenAIChatCompletionsProvider, OpenAITransport
from .protocol import TurnProvider
from .provider_config import openai_provider_config


@dataclass(frozen=True, slots=True)
class OpenAIModelProvider:
    name: str = "openai"
    config: OpenAIProviderConfig | None = None
    transport: OpenAITransport | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return openai_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        return OpenAIChatCompletionsProvider(
            name=self.name,
            config=self.config,
            transport=self.transport,
        )
