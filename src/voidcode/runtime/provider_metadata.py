from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from ..provider.model_catalog import ProviderModelMetadata as CatalogProviderModelMetadata
from ..provider.model_catalog import ToolFeedbackMode
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
    raw_items = cast(Iterable[object], value)
    items = tuple(item for item in raw_items if isinstance(item, str) and item)
    return items or None


def tool_feedback_mode(value: object) -> ToolFeedbackMode | None:
    if value in {"standard", "synthetic_user_message"}:
        return cast(ToolFeedbackMode, value)
    return None


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
        cost_per_cache_write_token=optional_positive_float(
            payload.get("cost_per_cache_write_token")
        ),
        supports_reasoning_effort=optional_bool(payload.get("supports_reasoning_effort")),
        default_reasoning_effort=optional_string(payload.get("default_reasoning_effort")),
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
    return ProviderModelMetadata(
        context_window=catalog_metadata.context_window,
        max_input_tokens=catalog_metadata.max_input_tokens,
        max_output_tokens=catalog_metadata.max_output_tokens,
        supports_tools=catalog_metadata.supports_tools,
        supports_vision=catalog_metadata.supports_vision,
        supports_streaming=catalog_metadata.supports_streaming,
        supports_reasoning=catalog_metadata.supports_reasoning,
        supports_json_mode=catalog_metadata.supports_json_mode,
        cost_per_input_token=catalog_metadata.cost_per_input_token,
        cost_per_output_token=catalog_metadata.cost_per_output_token,
        cost_per_cache_read_token=catalog_metadata.cost_per_cache_read_token,
        cost_per_cache_write_token=catalog_metadata.cost_per_cache_write_token,
        supports_reasoning_effort=catalog_metadata.supports_reasoning_effort,
        default_reasoning_effort=catalog_metadata.default_reasoning_effort,
        supports_reasoning_summary=catalog_metadata.supports_reasoning_summary,
        supports_thinking_budget=catalog_metadata.supports_thinking_budget,
        supports_interleaved_reasoning=catalog_metadata.supports_interleaved_reasoning,
        reasoning_visibility=catalog_metadata.reasoning_visibility,
        modalities_input=catalog_metadata.modalities_input,
        modalities_output=catalog_metadata.modalities_output,
        model_status=catalog_metadata.model_status,
        tool_feedback_mode=catalog_metadata.tool_feedback_mode,
    )


def contract_metadata_from_payload(payload: dict[str, object]) -> ProviderModelMetadata:
    return contract_metadata_from_catalog(catalog_metadata_from_payload(payload))


__all__ = [
    "catalog_metadata_from_payload",
    "contract_metadata_from_catalog",
    "contract_metadata_from_payload",
    "optional_bool",
    "optional_positive_float",
    "optional_positive_int",
    "optional_string",
    "optional_string_tuple",
    "tool_feedback_mode",
]
