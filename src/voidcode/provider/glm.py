from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


@dataclass(frozen=True, slots=True)
class GLMModelProvider:
    """GLM (智谱AI) Model Provider.

    GLM provides OpenAI-compatible API at https://open.bigmodel.cn/api/paas/v4

    Usage:
        providers:
          glm:
            api_key: "your-api-key"  # or set GLM_API_KEY env var
            model_map:
              glm-4: glm-4-flash  # optional model alias

    Environment Variables:
        GLM_API_KEY: API key for GLM authentication
    """

    name: str = "glm"
    config: SimplifiedProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
