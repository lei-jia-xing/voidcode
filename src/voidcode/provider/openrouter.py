from __future__ import annotations

from dataclasses import dataclass

from .config import ProviderEndpointConfig
from .openai_native import OpenAIChatCompletionsProvider
from .protocol import TurnProvider
from .provider_config import vendor_endpoint_config

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_API_KEY_ENV_VAR = "OPENROUTER_API_KEY"


@dataclass(frozen=True, slots=True)
class OpenRouterModelProvider:
    """OpenRouter's OpenAI-compatible chat-completions gateway."""

    name: str = "openrouter"
    config: ProviderEndpointConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return vendor_endpoint_config(
            self.config,
            base_url=_OPENROUTER_BASE_URL,
            api_key_env_var=_OPENROUTER_API_KEY_ENV_VAR,
        )

    def turn_provider(self) -> TurnProvider:
        return OpenAIChatCompletionsProvider(
            name=self.name,
            config=self.provider_config(),
        )
