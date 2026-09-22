"""Provider payload shaping shared by ``provider`` and ``config show``."""

from __future__ import annotations

from pathlib import Path

from ..runtime.contracts import ProviderInspectResult, ProviderModelMetadata, ProviderReadinessResult
from ..runtime.provider_inspection import ProviderEndpointFacts


def provider_model_metadata_payload(
    metadata: ProviderModelMetadata,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "context_window": metadata.context_window,
            "max_input_tokens": metadata.max_input_tokens,
            "max_output_tokens": metadata.max_output_tokens,
            "supports_tools": metadata.supports_tools,
            "supports_vision": metadata.supports_vision,
            "supports_streaming": metadata.supports_streaming,
            "supports_reasoning": metadata.supports_reasoning,
            "supports_json_mode": metadata.supports_json_mode,
            "cost_per_input_token": metadata.cost_per_input_token,
            "cost_per_output_token": metadata.cost_per_output_token,
            "cost_per_cache_read_token": metadata.cost_per_cache_read_token,
            "cost_per_cache_write_token": metadata.cost_per_cache_write_token,
            "supports_reasoning_effort": metadata.supports_reasoning_effort,
            "default_reasoning_effort": metadata.default_reasoning_effort,
            "supported_effort_levels": list(metadata.supported_effort_levels) if metadata.supported_effort_levels is not None else None,
            "supports_reasoning_summary": metadata.supports_reasoning_summary,
            "supports_thinking_budget": metadata.supports_thinking_budget,
            "supports_interleaved_reasoning": metadata.supports_interleaved_reasoning,
            "reasoning_visibility": metadata.reasoning_visibility,
            "modalities_input": list(metadata.modalities_input) if metadata.modalities_input is not None else None,
            "modalities_output": list(metadata.modalities_output) if metadata.modalities_output is not None else None,
            "model_status": metadata.model_status,
        }.items()
        if value is not None
    }


def provider_readiness_payload(readiness: ProviderReadinessResult) -> dict[str, object]:
    return {
        "provider": readiness.provider,
        "model": readiness.model,
        "configured": readiness.configured,
        "ok": readiness.ok,
        "status": readiness.status,
        "guidance": readiness.guidance,
        "auth_present": readiness.auth_present,
        "streaming_configured": readiness.streaming_configured,
        "streaming_supported": readiness.streaming_supported,
        "context_window": readiness.context_window,
        "max_output_tokens": readiness.max_output_tokens,
        "fallback_chain": list(readiness.fallback_chain),
        "reasoning_controls": readiness.reasoning_controls,
    }


def provider_inspect_payload(
    result: ProviderInspectResult,
    *,
    workspace: Path,
    endpoint: ProviderEndpointFacts,
) -> dict[str, object]:
    return {
        "workspace": str(workspace),
        "provider": {
            "name": result.summary.name,
            "label": result.summary.label,
            "configured": result.summary.configured,
            "current": result.summary.current,
        },
        # The endpoint this provider resolves to on the wire, and why: a config
        # block that named a base URL, the provider's own vendor default, or the
        # generic endpoint provider's local gateway.
        "endpoint": endpoint.as_payload(),
        "models": {
            "provider": result.models.provider,
            "configured": result.models.configured,
            "models": list(result.models.models),
            "model_metadata": {model: provider_model_metadata_payload(metadata) for model, metadata in result.models.model_metadata.items()},
            "source": result.models.source,
            "last_refresh_status": result.models.last_refresh_status,
            "last_error": result.models.last_error,
            "discovery_mode": result.models.discovery_mode,
        },
        "validation": {
            "provider": result.validation.provider,
            "configured": result.validation.configured,
            "ok": result.validation.ok,
            "status": result.validation.status,
            "message": result.validation.message,
            "source": result.validation.source,
            "last_error": result.validation.last_error,
            "discovery_mode": result.validation.discovery_mode,
            "failure_kind": result.validation.failure_kind,
            "guidance": result.validation.guidance,
        },
        "readiness": (provider_readiness_payload(result.readiness) if result.readiness is not None else None),
        "current_model": result.current_model,
        "current_model_metadata": (None if result.current_model_metadata is None else provider_model_metadata_payload(result.current_model_metadata)),
    }
