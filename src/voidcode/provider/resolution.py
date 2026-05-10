from __future__ import annotations

from .config import ProviderFallbackConfig
from .models import (
    ProviderModelSelection,
    ProviderResolutionMetadata,
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from .registry import ModelProviderRegistry


def _provider_fallback_with_preferred_model(
    provider_fallback: ProviderFallbackConfig,
    preferred_model: str,
) -> ProviderFallbackConfig:
    return ProviderFallbackConfig(
        preferred_model=preferred_model,
        fallback_models=tuple(
            fallback_model
            for fallback_model in provider_fallback.fallback_models
            if fallback_model != preferred_model
        ),
    )


def resolve_provider_model(
    raw_model: str | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderModel:
    if raw_model is None:
        return ResolvedProviderModel()

    provider_name, model_name = _parse_model_reference(raw_model)
    provider_resolution = registry.resolve_with_metadata(provider_name)
    return ResolvedProviderModel(
        selection=ProviderModelSelection(
            raw_model=raw_model,
            provider=provider_name,
            model=model_name,
        ),
        provider=provider_resolution.provider,
        resolution=ProviderResolutionMetadata(
            source=provider_resolution.source,
            configured=provider_resolution.configured,
        ),
        metadata=registry.model_metadata_for_model(provider_name, model_name),
    )


def resolve_provider_chain(
    provider_fallback: ProviderFallbackConfig | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderChain:
    if provider_fallback is None:
        return ResolvedProviderChain()

    _validate_unique_model_references(
        (provider_fallback.preferred_model, *provider_fallback.fallback_models)
    )
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


def resolve_provider_config(
    model: str | None,
    provider_fallback: ProviderFallbackConfig | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderConfig:
    if provider_fallback is not None:
        if model is not None and model != provider_fallback.preferred_model:
            provider_fallback = _provider_fallback_with_preferred_model(
                provider_fallback,
                model,
            )
        target_chain = resolve_provider_chain(provider_fallback, registry=registry)
        return ResolvedProviderConfig(
            model=provider_fallback.preferred_model,
            provider_fallback=provider_fallback,
            active_target=target_chain.preferred,
            target_chain=target_chain,
        )

    if model is None:
        return ResolvedProviderConfig()

    active_target = resolve_provider_model(model, registry=registry)
    target_chain = ResolvedProviderChain(
        preferred=active_target,
        all_targets=(active_target,),
    )
    return ResolvedProviderConfig(
        model=model,
        provider_fallback=None,
        active_target=active_target,
        target_chain=target_chain,
    )


def _parse_model_reference(raw_model: str) -> tuple[str, str]:
    provider_name, separator, model_name = raw_model.partition("/")
    if separator != "/" or not provider_name or not model_name:
        raise ValueError("model must use provider/model format")
    return provider_name, model_name


def _validate_unique_model_references(raw_models: tuple[str, ...]) -> None:
    if len(set(raw_models)) != len(raw_models):
        raise ValueError("provider fallback chain must not contain duplicate models")
