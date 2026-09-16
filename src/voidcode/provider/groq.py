from __future__ import annotations

from dataclasses import dataclass

from .config import OpenAICompatibleProviderConfig, openai_compatible_endpoint_config
from .openai_native import OpenAIChatCompletionsProvider
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class GroqModelProvider:
    """Groq's OpenAI-compatible inference API."""

    name: str = "groq"
    config: OpenAICompatibleProviderConfig | None = None

    def provider_config(self):
        return openai_compatible_endpoint_config(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = openai_compatible_endpoint_config(self.name, self.config)
        return OpenAIChatCompletionsProvider(name=self.name, config=adapted_config)
