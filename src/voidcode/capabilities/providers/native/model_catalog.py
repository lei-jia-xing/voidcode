from __future__ import annotations

from dataclasses import asdict, fields
from functools import lru_cache
from typing import Annotated, cast

from pydantic import ConfigDict, Field, TypeAdapter
from pydantic.dataclasses import dataclass as validated_dataclass

from ._resources import load_json
from .reasoning_effort import CANONICAL_EFFORTS

type TokenRate = Annotated[float | None, Field(strict=True, ge=0, allow_inf_nan=False)]
type OptionalLimit = Annotated[int | None, Field(strict=True, gt=0)]
type OptionalFlag = Annotated[bool | None, Field(strict=True)]
type OptionalText = Annotated[str | None, Field(strict=True, min_length=1)]
type TextItems = tuple[Annotated[str, Field(strict=True, min_length=1)], ...] | None


@validated_dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid"))
class ContextPriceTier:
    threshold: Annotated[int, Field(strict=True, gt=0)]
    input: TokenRate = None
    output: TokenRate = None
    cache_read: TokenRate = None
    cache_write: TokenRate = None


@validated_dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid"))
class ProviderModelMetadata:
    context_window: OptionalLimit = None
    max_input_tokens: OptionalLimit = None
    max_output_tokens: OptionalLimit = None
    supports_tools: OptionalFlag = None
    supports_vision: OptionalFlag = None
    supports_streaming: OptionalFlag = None
    supports_reasoning: OptionalFlag = None
    supports_json_mode: OptionalFlag = None
    cost_per_input_token: TokenRate = None
    cost_per_output_token: TokenRate = None
    cost_per_cache_read_token: TokenRate = None
    cost_per_cache_write_token: TokenRate = None
    context_price_tiers: tuple[ContextPriceTier, ...] = ()
    supports_reasoning_off: OptionalFlag = None
    reasoning_content_field: OptionalText = None
    supports_reasoning_effort: OptionalFlag = None
    default_reasoning_effort: OptionalText = None
    supported_effort_levels: TextItems = None
    supports_reasoning_summary: OptionalFlag = None
    supports_thinking_budget: OptionalFlag = None
    supports_interleaved_reasoning: OptionalFlag = None
    reasoning_visibility: OptionalText = None
    modalities_input: TextItems = None
    modalities_output: TextItems = None
    model_status: OptionalText = None
    api: OptionalText = None
    display_name: OptionalText = None

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


_MODEL_METADATA = TypeAdapter(ProviderModelMetadata)


def _static_catalog_from_payload(data: object) -> dict[str, dict[str, ProviderModelMetadata]]:
    if not isinstance(data, dict):
        raise ValueError("model_catalog_data.json must hold an object of providers")
    result: dict[str, dict[str, ProviderModelMetadata]] = {}
    for provider, models in cast(dict[str, object], data).items():
        provider_key = provider.strip().lower()
        if not provider_key or provider_key in result:
            raise ValueError(f"empty or duplicate normalized catalog provider: {provider!r}")
        if not isinstance(models, dict):
            raise ValueError(f"model catalog provider {provider!r} must hold an object of models")
        per_provider: dict[str, ProviderModelMetadata] = {}
        for model_id, entry in models.items():
            model_key = model_id.strip().lower()
            if not model_key or model_key in per_provider:
                raise ValueError(f"empty or duplicate normalized catalog model: {provider}/{model_id}")
            if not isinstance(entry, dict):
                raise ValueError(f"model catalog entry {provider}/{model_id} must be an object")
            try:
                per_provider[model_key] = _MODEL_METADATA.validate_python(entry)
            except ValueError as exc:
                raise ValueError(f"invalid model catalog entry {provider}/{model_id}: {exc}") from exc
        result[provider_key] = per_provider
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
