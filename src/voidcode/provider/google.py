from __future__ import annotations

from dataclasses import dataclass

from .config import GoogleProviderConfig, LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .litellm_config import google_provider_config
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class GoogleModelProvider:
    name: str = "google"
    config: GoogleProviderConfig | None = None

    def provider_config(self) -> LiteLLMProviderConfig:
        return google_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        api_key = None
        auth_header = None
        auth_scheme = "bearer"
        completion_kwargs: dict[str, object] = {}
        if self.config is not None and self.config.auth is not None:
            if self.config.auth.method == "api_key":
                api_key = self.config.auth.api_key
                auth_header = "x-goog-api-key"
                auth_scheme = "token"
            if self.config.auth.method == "oauth":
                api_key = self.config.auth.access_token
            if self.config.auth.method == "service_account":
                completion_kwargs["vertex_credentials"] = self.config.auth.service_account_json_path
        adapted_config = LiteLLMProviderConfig(
            api_key=api_key,
            base_url=None if self.config is None else self.config.base_url,
            auth_header=auth_header,
            auth_scheme=auth_scheme,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
        )
        return LiteLLMBackendSingleAgentProvider(
            name=self.name,
            config=adapted_config,
            completion_kwargs=completion_kwargs or None,
        )
