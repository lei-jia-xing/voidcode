from __future__ import annotations

from dataclasses import dataclass

from .config import CopilotProviderConfig, LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


@dataclass(frozen=True, slots=True)
class CopilotModelProvider:
    name: str = "copilot"
    config: CopilotProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        token = None
        if self.config is not None and self.config.auth is not None:
            token = self.config.auth.token
        adapted_config = LiteLLMProviderConfig(
            api_key=token,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
        )
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
