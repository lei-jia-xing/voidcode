from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig, SimplifiedProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


@dataclass(frozen=True, slots=True)
class KimiModelProvider:
    """Kimi (Moonshot AI) Model Provider.

    Kimi provides OpenAI-compatible API at https://api.moonshot.cn/v1 (China)
    or https://api.moonshot.ai/v1 (Global)

    Usage:
        providers:
          kimi:
            api_key: "your-api-key"  # or set KIMI_API_KEY env var
            model_map:
              k2.5: kimi-k2.5  # optional model alias

    Environment Variables:
        KIMI_API_KEY: API key for Kimi authentication
    """

    name: str = "kimi"
    config: SimplifiedProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        adapted_config = LiteLLMProviderConfig(
            api_key=None if self.config is None else self.config.api_key,
            base_url=None if self.config is None else self.config.base_url,
            timeout_seconds=None if self.config is None else self.config.timeout_seconds,
            model_map={} if self.config is None else self.config.model_map,
        )
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
