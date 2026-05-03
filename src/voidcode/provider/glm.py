from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import TurnProvider


@dataclass(frozen=True, slots=True)
class GLMModelProvider:
    """GLM (智谱AI) Model Provider.

    GLM provides OpenAI-compatible API at https://open.bigmodel.cn/api/paas/v4

    Usage:
        providers:
          glm:
            api_key: "your-api-key"  # or set ZAI_API_KEY / ZHIPU_API_KEY env vars
            model_map:
              glm-4: glm-4-flash  # optional model alias

    Environment Variables:
        ZAI_API_KEY: API key for GLM authentication via ZAI port
        ZHIPU_API_KEY: API key for GLM authentication via ZHIPU port
        GLM_API_KEY: optional fallback API key for compatibility
    """

    name: str = "glm"
    config: SimplifiedProviderConfig | None = None

    def provider_config(self):
        return simplified_config_to_litellm(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
