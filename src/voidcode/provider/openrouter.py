from __future__ import annotations

from dataclasses import dataclass

from .config import ProviderEndpointConfig
from .openai_native import OpenAIChatCompletionsProvider
from .protocol import TurnProvider
from .provider_config import vendor_endpoint_config
from .provider_table import PROVIDER_TABLE_BY_ID

_OPENROUTER = PROVIDER_TABLE_BY_ID["openrouter"]


@dataclass(frozen=True, slots=True)
class OpenRouterModelProvider:
    """OpenRouter's OpenAI-compatible chat-completions gateway."""

    name: str = "openrouter"
    config: ProviderEndpointConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return vendor_endpoint_config(
            self.config,
            base_url=_OPENROUTER.default_base_url,
            api_key_env_var=_OPENROUTER.env_vars[0],
        )

    def turn_provider(self) -> TurnProvider:
        return OpenAIChatCompletionsProvider(
            name=self.name,
            config=self.provider_config(),
        )
