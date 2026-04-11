from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .config import RuntimeProviderFallbackConfig
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


@dataclass(frozen=True, slots=True)
class ResolvedProviderChain:
    preferred: ResolvedProviderModel = ResolvedProviderModel()
    fallbacks: tuple[ResolvedProviderModel, ...] = ()
    all_targets: tuple[ResolvedProviderModel, ...] = ()

    def target_at(self, index: int) -> ResolvedProviderModel | None:
        if index < 0 or index >= len(self.all_targets):
            return None
        return self.all_targets[index]


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


def resolve_provider_chain(
    provider_fallback: RuntimeProviderFallbackConfig | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderChain:
    if provider_fallback is None:
        return ResolvedProviderChain()

    preferred = resolve_provider_model(provider_fallback.preferred_model, registry=registry)
    fallbacks = tuple(
        resolve_provider_model(raw_model, registry=registry)
        for raw_model in provider_fallback.fallback_models
    )
    return ResolvedProviderChain(
        preferred=preferred,
        fallbacks=fallbacks,
        all_targets=(preferred, *fallbacks),
    )


def _parse_model_reference(raw_model: str) -> tuple[str, str]:
    provider_name, separator, model_name = raw_model.partition("/")
    if separator != "/" or "/" in model_name or not provider_name or not model_name:
        raise ValueError("model must use provider/model format")
    return provider_name, model_name
