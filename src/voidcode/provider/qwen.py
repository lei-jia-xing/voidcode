from __future__ import annotations

from dataclasses import dataclass

from .simplified import SimplifiedModelProvider


@dataclass(frozen=True, slots=True)
class QwenModelProvider(SimplifiedModelProvider):
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
