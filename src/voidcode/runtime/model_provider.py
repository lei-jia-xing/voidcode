from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .single_agent_provider import SingleAgentProvider, StubSingleAgentProvider


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def single_agent_provider(self) -> SingleAgentProvider: ...


@dataclass(frozen=True, slots=True)
class StaticModelProvider:
    name: str

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)


@dataclass(frozen=True, slots=True)
class ProviderModelSelection:
    raw_model: str | None = None
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProviderModel:
    selection: ProviderModelSelection = ProviderModelSelection()
    provider: ModelProvider | None = None


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelProvider]

    @classmethod
    def with_defaults(cls) -> ModelProviderRegistry:
        return cls(providers={"opencode": StaticModelProvider(name="opencode")})

    def resolve(self, provider_name: str) -> ModelProvider:
        return self.providers.get(provider_name, StaticModelProvider(name=provider_name))


def resolve_provider_model(
    raw_model: str | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderModel:
    if raw_model is None:
        return ResolvedProviderModel()

    provider_name, model_name = _parse_model_reference(raw_model)
    provider = registry.resolve(provider_name)
    return ResolvedProviderModel(
        selection=ProviderModelSelection(
            raw_model=raw_model,
            provider=provider_name,
            model=model_name,
        ),
        provider=provider,
    )


def _parse_model_reference(raw_model: str) -> tuple[str, str]:
    provider_name, separator, model_name = raw_model.partition("/")
    if separator != "/" or "/" in model_name or not provider_name or not model_name:
        raise ValueError("model must use provider/model format")
    return provider_name, model_name
