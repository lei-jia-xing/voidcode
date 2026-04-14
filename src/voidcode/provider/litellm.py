from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


@dataclass(frozen=True, slots=True)
class LiteLLMModelProvider:
    name: str = "litellm"
    config: LiteLLMProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=self.config)
