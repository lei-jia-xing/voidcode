from __future__ import annotations

from dataclasses import dataclass

from .simplified import SimplifiedModelProvider


@dataclass(frozen=True, slots=True)
class DeepSeekModelProvider(SimplifiedModelProvider):
    """DeepSeek OpenAI-compatible model provider."""

    name: str = "deepseek"
