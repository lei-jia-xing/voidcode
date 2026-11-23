from __future__ import annotations

from dataclasses import dataclass

from voidcode.sdk.provider import TurnProvider

from ..registry import PROVIDER_TABLE_BY_ID
from .config import ProviderEndpointConfig
from .openai_native import OpenAIChatCompletionsProvider
from .provider_config import vendor_endpoint_config

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
        )

    def turn_provider(self) -> TurnProvider:
        return OpenAIChatCompletionsProvider(
            name=self.name,
            config=self.provider_config(),
        )
