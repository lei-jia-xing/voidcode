from __future__ import annotations

from dataclasses import dataclass

from .config import OpenAICompatibleProviderConfig, openai_compatible_config_to_litellm
from .litellm_backend import LiteLLMBackendProvider
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class FireworksModelProvider:
    """Fireworks AI's OpenAI-compatible inference API."""

    name: str = "fireworks"
    config: OpenAICompatibleProviderConfig | None = None

    def provider_config(self):
        return openai_compatible_config_to_litellm(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = openai_compatible_config_to_litellm(self.name, self.config)
        return LiteLLMBackendProvider(name=self.name, config=adapted_config)
