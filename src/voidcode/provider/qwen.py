from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class QwenModelProvider:
    """Qwen (通义千问) Model Provider.

    Qwen provides OpenAI-compatible API at https://dashscope.aliyuncs.com/compatible-mode/v1

    Usage:
        providers:
          qwen:
            api_key: "your-api-key"  # or set DASHSCOPE_API_KEY env var
            model_map:
              qwen-plus: qwen-plus  # optional model alias

    Environment Variables:
        DASHSCOPE_API_KEY: API key for Qwen authentication
    """

    name: str = "qwen"
    config: SimplifiedProviderConfig | None = None

    def provider_config(self):
        return simplified_config_to_litellm(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
