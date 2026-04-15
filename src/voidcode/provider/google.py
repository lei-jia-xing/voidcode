from __future__ import annotations

from dataclasses import dataclass

from .config import GoogleProviderConfig, LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


@dataclass(frozen=True, slots=True)
class GoogleModelProvider:
    name: str = "google"
    config: GoogleProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        api_key = None
        completion_kwargs: dict[str, object] = {}
        if self.config is not None and self.config.auth is not None:
            if self.config.auth.method == "api_key":
                api_key = self.config.auth.api_key
            if self.config.auth.method == "oauth":
                api_key = self.config.auth.access_token
            if self.config.auth.method == "service_account":
                completion_kwargs["vertex_credentials"] = self.config.auth.service_account_json_path
        adapted_config = LiteLLMProviderConfig(
            api_key=api_key,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
        )
        return LiteLLMBackendSingleAgentProvider(
            name=self.name,
            config=adapted_config,
            completion_kwargs=completion_kwargs or None,
        )
