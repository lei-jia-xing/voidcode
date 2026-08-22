from __future__ import annotations

from dataclasses import dataclass

from .config import AnthropicProviderConfig, LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .litellm_config import anthropic_provider_config
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class AnthropicModelProvider:
    name: str = "anthropic"
    config: AnthropicProviderConfig | None = None

    def provider_config(self) -> LiteLLMProviderConfig:
        return anthropic_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        return LiteLLMBackendSingleAgentProvider(
            name=self.name,
            config=self.provider_config(),
        )
