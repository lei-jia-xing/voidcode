from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from math import isfinite
from typing import Annotated, Final, Literal, TypeIs, cast

from pydantic import ConfigDict, Field, TypeAdapter
from pydantic.dataclasses import dataclass as validated_dataclass

from ._resources import load_json
from .reasoning_effort import CANONICAL_EFFORTS

type ToolFeedbackMode = Literal["standard", "synthetic_user_message"]

_TOOL_FEEDBACK_MODES: Final[tuple[ToolFeedbackMode, ...]] = ("standard", "synthetic_user_message")


def is_tool_feedback_mode(value: object) -> TypeIs[ToolFeedbackMode]:
    """Whether an untrusted ``tool_feedback_mode`` token names one of the known modes."""
    return value in _TOOL_FEEDBACK_MODES


def tool_feedback_mode(value: object) -> ToolFeedbackMode | None:
    """The declared tool-feedback mode, ``None`` when the value declares none."""
    return value if is_tool_feedback_mode(value) else None


type TokenRate = Annotated[float | None, Field(strict=True, ge=0, allow_inf_nan=False)]


@validated_dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid"))
class ContextPriceTier:
    threshold: Annotated[int, Field(strict=True, gt=0)]
    input: TokenRate = None
    output: TokenRate = None
    cache_read: TokenRate = None
    cache_write: TokenRate = None


_CONTEXT_PRICE_TIERS = TypeAdapter(tuple[ContextPriceTier, ...])


@dataclass(frozen=True, slots=True)
class ProviderModelMetadata:
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    supports_reasoning: bool | None = None
    supports_json_mode: bool | None = None
    cost_per_input_token: float | None = None
    cost_per_output_token: float | None = None
    cost_per_cache_read_token: float | None = None
    cost_per_cache_write_token: float | None = None
    context_price_tiers: tuple[ContextPriceTier, ...] = ()
    supports_reasoning_off: bool | None = None
    reasoning_content_field: str | None = None
    supports_reasoning_effort: bool | None = None
    default_reasoning_effort: str | None = None
    supported_effort_levels: tuple[str, ...] | None = None
    supports_reasoning_summary: bool | None = None
    supports_thinking_budget: bool | None = None
    supports_interleaved_reasoning: bool | None = None
    reasoning_visibility: str | None = None
    modalities_input: tuple[str, ...] | None = None
    modalities_output: tuple[str, ...] | None = None
    model_status: str | None = None
    tool_feedback_mode: ToolFeedbackMode | None = None
    api: str | None = None
    display_name: str | None = None
    #: omp encoding name driving exact token counting (``O200kBase``,
    #: ``deepseek-v3``, ...). ``None`` means no exact tokenizer and the runtime
    #: falls back to the byte-count estimate. See ``provider/tokenizer.py``.
    tokenizer: str | None = None

    def __post_init__(self) -> None:
        if self.default_reasoning_effort is not None and self.default_reasoning_effort not in CANONICAL_EFFORTS:
            raise ValueError(f"default_reasoning_effort must be one of {CANONICAL_EFFORTS}; got {self.default_reasoning_effort!r}")

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        for model_field in fields(self):
            value = getattr(self, model_field.name)
            if value is None or (model_field.name == "context_price_tiers" and not value):
                continue
            if model_field.name == "context_price_tiers":
                payload[model_field.name] = [asdict(tier) for tier in self.context_price_tiers]
                continue
            payload[model_field.name] = list(value) if isinstance(value, tuple) else value
        return payload


@lru_cache(maxsize=1)
def _load_static_catalog() -> dict[str, dict[str, ProviderModelMetadata]]:
    # A packaged resource: a missing file means a broken install, not an empty
    # catalog, so the read stays loud instead of quietly reporting no metadata.
    return _static_catalog_from_payload(load_json("model_catalog_data.json"))


#: Every key a shipped catalog entry may carry: the loader constructs
#: ``ProviderModelMetadata`` from them, so a key outside this set -- a typo, or a
#: generator that started emitting a new field without the loader -- would be
#: silently dropped instead of read.
_CATALOG_FIELDS: Final[frozenset[str]] = frozenset(model_field.name for model_field in fields(ProviderModelMetadata))


def _static_catalog_from_payload(data: object) -> dict[str, dict[str, ProviderModelMetadata]]:
    if not isinstance(data, dict):
        raise ValueError("model_catalog_data.json must hold an object of providers")
    result: dict[str, dict[str, ProviderModelMetadata]] = {}
    for provider, models in cast(dict[str, object], data).items():
        if not isinstance(models, dict):
            raise ValueError(f"model catalog provider {provider!r} must hold an object of models")
        per_provider: dict[str, ProviderModelMetadata] = {}
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                raise ValueError(f"model catalog entry {provider}/{model_id} must be an object")
            unknown = sorted(set(entry) - _CATALOG_FIELDS)
            if unknown:
                raise ValueError(f"model catalog entry {provider}/{model_id} carries unknown fields: {unknown}")
            per_provider[model_id.strip().lower()] = ProviderModelMetadata(
                context_window=_positive_int(entry.get("context_window")),
                max_input_tokens=_positive_int(entry.get("max_input_tokens")),
                max_output_tokens=_positive_int(entry.get("max_output_tokens")),
                cost_per_input_token=_non_negative_float(entry.get("cost_per_input_token")),
                cost_per_output_token=_non_negative_float(entry.get("cost_per_output_token")),
                cost_per_cache_read_token=_non_negative_float(entry.get("cost_per_cache_read_token")),
                cost_per_cache_write_token=_non_negative_float(entry.get("cost_per_cache_write_token")),
                supports_tools=_optional_bool(entry.get("supports_tools")),
                supports_reasoning=_optional_bool(entry.get("supports_reasoning")),
                context_price_tiers=_CONTEXT_PRICE_TIERS.validate_python(entry.get("context_price_tiers", ())),
                supports_reasoning_off=_optional_bool(entry.get("supports_reasoning_off")),
                reasoning_content_field=_optional_str(entry.get("reasoning_content_field")),
                supports_reasoning_effort=_optional_bool(entry.get("supports_reasoning_effort")),
                default_reasoning_effort=_canonical_effort(entry.get("default_reasoning_effort")),
                supported_effort_levels=_modalities(entry.get("supported_effort_levels")),
                supports_vision=_optional_bool(entry.get("supports_vision")),
                supports_streaming=_optional_bool(entry.get("supports_streaming")),
                supports_json_mode=_optional_bool(entry.get("supports_json_mode")),
                supports_reasoning_summary=_optional_bool(entry.get("supports_reasoning_summary")),
                supports_thinking_budget=_optional_bool(entry.get("supports_thinking_budget")),
                supports_interleaved_reasoning=_optional_bool(entry.get("supports_interleaved_reasoning")),
                reasoning_visibility=_optional_str(entry.get("reasoning_visibility")),
                tool_feedback_mode=tool_feedback_mode(entry.get("tool_feedback_mode")),
                modalities_input=_modalities(entry.get("modalities_input")),
                modalities_output=_modalities(entry.get("modalities_output")),
                model_status=_optional_str(entry.get("model_status")),
                api=_optional_str(entry.get("api")),
                display_name=_optional_str(entry.get("display_name")),
                tokenizer=_optional_str(entry.get("tokenizer")),
            )
        result[provider.strip().lower()] = per_provider
    return result


def static_catalog_metadata(provider_name: str, model_name: str) -> ProviderModelMetadata | None:
    """Shipped metadata used by routing, effort selection and usage pricing."""
    provider = provider_name.strip().lower()
    model = model_name.strip().lower()
    if not model:
        return None
    return _load_static_catalog().get(provider, {}).get(model)


def static_catalog_model_ids(provider_name: str) -> tuple[str, ...]:
    """Shipped catalog model ids for one provider, sorted; empty when it ships none."""
    return tuple(sorted(_load_static_catalog().get(provider_name.strip().lower(), {})))


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _non_negative_float(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value) and value >= 0:
        return float(value)
    return None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _modalities(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    raw_items = value
    modalities = tuple(item for item in raw_items if isinstance(item, str) and item)
    return modalities or None


def _canonical_effort(value: object) -> str | None:
    effort = _optional_str(value)
    if effort is None or effort not in CANONICAL_EFFORTS:
        return None
    return effort
