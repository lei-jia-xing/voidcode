from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal

from ..provider.model_catalog import ProviderModelMetadata as CatalogProviderModelMetadata
from ..provider.model_catalog import tool_feedback_mode
from .config_materializer import EffectiveRuntimeConfig
from .contracts import ProviderModelMetadata


def optional_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def optional_positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    normalized = float(value)
    return normalized if normalized > 0 else None


def optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def optional_string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list | tuple):
        return None
    items = tuple(item for item in value if isinstance(item, str) and item)
    return items or None


def catalog_metadata_from_payload(
    payload: dict[str, object],
) -> CatalogProviderModelMetadata:
    return CatalogProviderModelMetadata(
        context_window=optional_positive_int(payload.get("context_window")),
        max_input_tokens=optional_positive_int(payload.get("max_input_tokens")),
        max_output_tokens=optional_positive_int(payload.get("max_output_tokens")),
        supports_tools=optional_bool(payload.get("supports_tools")),
        supports_vision=optional_bool(payload.get("supports_vision")),
        supports_streaming=optional_bool(payload.get("supports_streaming")),
        supports_reasoning=optional_bool(payload.get("supports_reasoning")),
        supports_json_mode=optional_bool(payload.get("supports_json_mode")),
        cost_per_input_token=optional_positive_float(payload.get("cost_per_input_token")),
        cost_per_output_token=optional_positive_float(payload.get("cost_per_output_token")),
        cost_per_cache_read_token=optional_positive_float(payload.get("cost_per_cache_read_token")),
        cost_per_cache_write_token=optional_positive_float(payload.get("cost_per_cache_write_token")),
        supports_reasoning_effort=optional_bool(payload.get("supports_reasoning_effort")),
        default_reasoning_effort=optional_string(payload.get("default_reasoning_effort")),
        supported_effort_levels=optional_string_tuple(payload.get("supported_effort_levels")),
        supports_reasoning_summary=optional_bool(payload.get("supports_reasoning_summary")),
        supports_thinking_budget=optional_bool(payload.get("supports_thinking_budget")),
        supports_interleaved_reasoning=optional_bool(payload.get("supports_interleaved_reasoning")),
        reasoning_visibility=optional_string(payload.get("reasoning_visibility")),
        modalities_input=optional_string_tuple(payload.get("modalities_input")),
        modalities_output=optional_string_tuple(payload.get("modalities_output")),
        model_status=optional_string(payload.get("model_status")),
        tool_feedback_mode=tool_feedback_mode(payload.get("tool_feedback_mode")),
    )


def contract_metadata_from_catalog(
    catalog_metadata: CatalogProviderModelMetadata,
) -> ProviderModelMetadata:
    return ProviderModelMetadata(**{field.name: getattr(catalog_metadata, field.name) for field in fields(ProviderModelMetadata) if field.init})


__all__ = [
    "ReasoningEffortCapability",
    "ReasoningEffortCapabilitySource",
    "catalog_metadata_from_payload",
    "contract_metadata_from_catalog",
    "resolve_reasoning_effort_capability",
    "tool_feedback_mode",
    "validate_reasoning_effort_capability",
]


type ReasoningEffortCapabilitySource = Literal["model_metadata", "unknown"]


@dataclass(frozen=True, slots=True)
class ReasoningEffortCapability:
    """Whether the resolved model accepts a reasoning-effort hint, and where that verdict came from.

    ``supported`` is the verdict; ``source`` records its provenance so callers can
    report it honestly instead of blaming the wrong layer:

    - ``model_metadata``: the model's own capability, from the provider catalog.
    - ``unknown``: the catalog is silent for this model. Callers forward
      best-effort and must record that the capability was unverified. There is no
      provider-level fallback: capability is a property of the model.
    """

    supported: bool | None
    source: ReasoningEffortCapabilitySource


def resolve_reasoning_effort_capability(
    *,
    provider_name: str | None,
    model_name: str | None,
    model_metadata: ProviderModelMetadata | None,
) -> ReasoningEffortCapability:
    """Resolve reasoning-effort capability for one resolved provider/model target.

    Reasoning effort belongs to the model, so model metadata wins. The provider
    allowlist is a fallback only: a provider name must never override a model
    that declares its own capability.
    """
    _ = provider_name, model_name  # kept in the signature: callers report them alongside the verdict
    if model_metadata is not None and model_metadata.supports_reasoning_effort is not None:
        return ReasoningEffortCapability(
            supported=model_metadata.supports_reasoning_effort,
            source="model_metadata",
        )
    return ReasoningEffortCapability(supported=None, source="unknown")


def validate_reasoning_effort_capability(
    config: EffectiveRuntimeConfig,
    capability: ReasoningEffortCapability,
) -> None:
    """Fail fast when the effective config asks for an effort the target cannot take."""
    if config.reasoning_effort is None:
        return
    if config.execution_engine != "provider":
        return
    if capability.supported is not False:
        return
    active_target = config.resolved_provider.active_target.selection
    raise ValueError(
        "reasoning_effort is configured but model "
        f"'{active_target.provider}/{active_target.model}' does not support reasoning effort; "
        "remove the reasoning_effort hint or pick a reasoning-effort capable model"
    )
