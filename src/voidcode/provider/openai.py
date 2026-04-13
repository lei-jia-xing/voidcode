from __future__ import annotations

from dataclasses import dataclass

from .protocol import SingleAgentProvider, StubSingleAgentProvider


@dataclass(frozen=True, slots=True)
class OpenAIModelProvider:
    name: str = "openai"

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)
