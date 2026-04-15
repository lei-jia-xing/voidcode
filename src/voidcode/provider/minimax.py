from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig, SimplifiedProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


@dataclass(frozen=True, slots=True)
class MiniMaxModelProvider:
    """MiniMax AI Model Provider.

    MiniMax provides OpenAI-compatible and Anthropic-compatible APIs.
    - OpenAI: https://api.minimax.io/v1
    - Anthropic: https://api.minimax.io/anthropic

    Usage:
        providers:
          minimax:
            api_key: "your-api-key"  # or set MINIMAX_API_KEY env var
            model_map:
              m2.5: MiniMax-M2.5  # optional model alias

    Environment Variables:
        MINIMAX_API_KEY: API key for MiniMax authentication
    """

    name: str = "minimax"
    config: SimplifiedProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        adapted_config = LiteLLMProviderConfig(
            api_key=None if self.config is None else self.config.api_key,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
            model_map={} if self.config is None else self.config.model_map,
        )
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
