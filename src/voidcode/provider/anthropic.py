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
        adapted_config = LiteLLMProviderConfig(
            api_key=None if self.config is None else self.config.api_key,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
        )
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
