from __future__ import annotations

from dataclasses import dataclass

from .simplified import SimplifiedModelProvider


@dataclass(frozen=True, slots=True)
class GrokModelProvider(SimplifiedModelProvider):
    """xAI Grok OpenAI-compatible model provider."""

    name: str = "grok"
