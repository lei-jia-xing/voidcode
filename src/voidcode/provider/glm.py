from __future__ import annotations

from dataclasses import dataclass

from .simplified import SimplifiedModelProvider


@dataclass(frozen=True, slots=True)
class GLMModelProvider(SimplifiedModelProvider):
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
