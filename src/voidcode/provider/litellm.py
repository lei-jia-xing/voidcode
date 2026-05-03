from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .litellm_config import litellm_provider_config
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class LiteLLMModelProvider:
    name: str = "litellm"
    config: LiteLLMProviderConfig | None = None

    def provider_config(self) -> LiteLLMProviderConfig:
        return litellm_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=self.config)
