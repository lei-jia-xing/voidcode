from __future__ import annotations

from collections.abc import Mapping

from .config import RuntimeAgentConfig, RuntimeProviderFallbackConfig


def delegated_model_for_route_from_configs(
    *,
    selected_preset: str,
    request_agent: RuntimeAgentConfig | None,
    agents: Mapping[str, RuntimeAgentConfig],
    base_model: str | None,
) -> str | None:
    if request_agent is not None and request_agent.model is not None:
        return request_agent.model
    preset_agent = agents.get(selected_preset)
    if preset_agent is not None and preset_agent.model is not None:
        return preset_agent.model
    return base_model


def provider_fallback_with_preferred_model(
    provider_fallback: RuntimeProviderFallbackConfig,
    preferred_model: str,
) -> RuntimeProviderFallbackConfig:
    return RuntimeProviderFallbackConfig(
        preferred_model=preferred_model,
        fallback_models=tuple(fallback_model for fallback_model in provider_fallback.fallback_models if fallback_model != preferred_model),
    )


def provider_fallback_for_agent_selection(
    *,
    model: str | None,
    preset_agent: RuntimeAgentConfig | None,
    base_provider_fallback: RuntimeProviderFallbackConfig | None,
) -> RuntimeProviderFallbackConfig | None:
    if preset_agent is not None and preset_agent.provider_fallback is not None:
        if model is None or model == preset_agent.provider_fallback.preferred_model:
            return preset_agent.provider_fallback
        return provider_fallback_with_preferred_model(
            preset_agent.provider_fallback,
            model,
        )
    if base_provider_fallback is None:
        return None
    if model is None or model == base_provider_fallback.preferred_model:
        return base_provider_fallback
    return provider_fallback_with_preferred_model(base_provider_fallback, model)


__all__ = [
    "delegated_model_for_route_from_configs",
    "provider_fallback_for_agent_selection",
    "provider_fallback_with_preferred_model",
]
