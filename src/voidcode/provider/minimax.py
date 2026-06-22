from __future__ import annotations

from dataclasses import dataclass

from .simplified import SimplifiedModelProvider


@dataclass(frozen=True, slots=True)
class MiniMaxModelProvider(SimplifiedModelProvider):
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
