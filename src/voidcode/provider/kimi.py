from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import TurnProvider


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

    def provider_config(self):
        return simplified_config_to_litellm(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
