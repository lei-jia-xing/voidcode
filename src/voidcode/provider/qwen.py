from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig, SimplifiedProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


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

    def single_agent_provider(self) -> SingleAgentProvider:
        adapted_config = LiteLLMProviderConfig(
            api_key=None if self.config is None else self.config.api_key,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
            model_map={} if self.config is None else self.config.model_map,
        )
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
