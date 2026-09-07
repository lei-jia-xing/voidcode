from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class MistralModelProvider:
    """Mistral's OpenAI-compatible chat-completions API."""

    name: str = "mistral"
    config: SimplifiedProviderConfig | None = None

    def provider_config(self):
        return simplified_config_to_litellm(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
