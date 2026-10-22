from __future__ import annotations

from dataclasses import dataclass

from voidcode.sdk.provider import TurnProvider

from .config import GoogleProviderConfig, ProviderEndpointConfig
from .google_native import GoogleGenAIProvider
from .provider_config import google_provider_config


@dataclass(frozen=True, slots=True)
class GoogleModelProvider:
    name: str = "google"
    config: GoogleProviderConfig | None = None

    def provider_config(self) -> ProviderEndpointConfig:
        return google_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        return GoogleGenAIProvider(name=self.name, config=self.config)
