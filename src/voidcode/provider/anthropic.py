from __future__ import annotations

from dataclasses import dataclass

from .config import AnthropicProviderConfig, LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


@dataclass(frozen=True, slots=True)
class AnthropicModelProvider:
    name: str = "anthropic"
    config: AnthropicProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        adapted_config = LiteLLMProviderConfig(
            api_key=None if self.config is None else self.config.api_key,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
        )
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
