from __future__ import annotations

from dataclasses import dataclass

from .protocol import ModelProvider, SingleAgentProvider, StubSingleAgentProvider


@dataclass(frozen=True, slots=True)
class StaticModelProvider:
    name: str

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelProvider]

    @classmethod
    def with_defaults(cls) -> ModelProviderRegistry:
        return cls(providers={"opencode": StaticModelProvider(name="opencode")})

    def resolve(self, provider_name: str) -> ModelProvider:
        return self.providers.get(provider_name, StaticModelProvider(name=provider_name))
