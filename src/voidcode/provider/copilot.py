from __future__ import annotations

from dataclasses import dataclass

from .config import CopilotProviderConfig, LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .litellm_config import copilot_provider_config
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class CopilotModelProvider:
    name: str = "copilot"
    config: CopilotProviderConfig | None = None

    def provider_config(self) -> LiteLLMProviderConfig:
        return copilot_provider_config(self.config)

    def turn_provider(self) -> TurnProvider:
        token = None
        if self.config is not None and self.config.auth is not None:
            token = self.config.auth.token
            if token is None and self.config.auth.token_env_var is not None:
                import os

                token = os.environ.get(self.config.auth.token_env_var)
        adapted_config = LiteLLMProviderConfig(
            api_key=token,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
        )
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
