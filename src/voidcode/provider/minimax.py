from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import TurnProvider


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

    def provider_config(self):
        return simplified_config_to_litellm(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
