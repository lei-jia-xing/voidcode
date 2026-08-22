from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig, OpenAIProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .litellm_config import openai_provider_config
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class OpenAIModelProvider:
    name: str = "openai"
    config: OpenAIProviderConfig | None = None

    def provider_config(self) -> LiteLLMProviderConfig:
        return openai_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        return LiteLLMBackendSingleAgentProvider(
            name=self.name,
            config=self.provider_config(),
        )
