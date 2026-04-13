from __future__ import annotations

from dataclasses import dataclass

from .protocol import SingleAgentProvider, StubSingleAgentProvider


@dataclass(frozen=True, slots=True)
class GoogleModelProvider:
    name: str = "google"

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)
