from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class LiteLLMModelProvider:
    name: str = "litellm"
    config: LiteLLMProviderConfig | None = None

    def turn_provider(self) -> TurnProvider:
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=self.config)
