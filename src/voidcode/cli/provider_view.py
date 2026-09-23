"""Provider payload shaping shared by ``provider`` and ``config show``."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..runtime.contracts import ProviderInspectResult, ProviderModelMetadata, ProviderReadinessResult
from ..runtime.provider_inspection import ProviderEndpointFacts


def provider_model_metadata_payload(
    metadata: ProviderModelMetadata,
) -> dict[str, object]:
    return {key: value for key, value in asdict(metadata).items() if value is not None and key != "tool_feedback_mode"}


def provider_readiness_payload(readiness: ProviderReadinessResult) -> dict[str, object]:
    return asdict(readiness)


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
