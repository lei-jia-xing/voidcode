from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .config import ProviderFallbackConfig
from .errors import format_invalid_provider_config_error, validation_reason_from_error
from .models import BoundProviderConfig, BoundProviderModel, ResolvedProviderChain, ResolvedProviderConfig, ResolvedProviderModel
from .naming import split_provider_model_reference
from .registry import ModelProviderRegistry
from .resolution import resolve_provider_model


class _ResolvedProviderTargetSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_model: str
    provider: str
    model: str

    @field_validator("raw_model", "provider", "model", mode="before")
    @classmethod
    def _validate_required_string(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a nonempty string")
        return value


class _ResolvedProviderSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    active_target: _ResolvedProviderTargetSnapshotPayload
    targets: tuple[_ResolvedProviderTargetSnapshotPayload, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_version(cls, value: object) -> int:
        if type(value) is not int or value != 2:
            raise ValueError("must be integer 2")
        return value

    @field_validator("targets", mode="before")
    @classmethod
    def _validate_targets_array(cls, value: object) -> list[object]:
        if not isinstance(value, list):
            raise ValueError("must be an array")
        return value

    @field_validator("targets", mode="after")
    @classmethod
    def _validate_targets_not_empty(
        cls,
        value: tuple[_ResolvedProviderTargetSnapshotPayload, ...],
    ) -> tuple[_ResolvedProviderTargetSnapshotPayload, ...]:
        if not value:
            raise ValueError("must not be empty")
        return value


def _format_snapshot_validation_error(*, source: str, error: dict[str, object]) -> str:
    loc = tuple(cast(tuple[object, ...], error.get("loc", ())))
    error_type = error.get("type", "")
    field_path = source
    for item in loc:
        if isinstance(item, int):
            field_path = f"{field_path}[{item}]"
            continue
        field_path = f"{field_path}.{item}"
    if error_type in {"model_type", "dict_type"}:
        return format_invalid_provider_config_error(field_path, "must be an object")
    return format_invalid_provider_config_error(field_path, validation_reason_from_error(error))


def resolved_provider_snapshot(resolved_provider: ResolvedProviderConfig | BoundProviderConfig | None) -> dict[str, object] | None:
    if resolved_provider is None:
        return None
    active_target = resolved_provider.active_target
    if not resolved_provider.target_chain.all_targets:
        if resolved_provider.model is not None or resolved_provider.provider_fallback is not None:
            raise ValueError("provider configuration has no target chain")
        if active_target is not None and active_target.selection.raw_model is not None:
            raise ValueError("active provider target has no target chain")
        return None
    if active_target is None:
        raise ValueError("provider target chain has no active target")
    targets = [_resolved_provider_target_snapshot(target) for target in resolved_provider.target_chain.all_targets]
    active_snapshot = _resolved_provider_target_snapshot(active_target)
    if active_snapshot not in targets:
        raise ValueError("active provider target must belong to its selected chain")
    return {"schema_version": 2, "active_target": active_snapshot, "targets": targets}


def parse_resolved_provider_snapshot(
    raw_snapshot: object,
    *,
    source: str,
    registry: ModelProviderRegistry,
) -> ResolvedProviderConfig:
    try:
        snapshot = _ResolvedProviderSnapshotPayload.model_validate(raw_snapshot)
    except ValidationError as exc:
        error = cast(dict[str, object], exc.errors(include_url=False)[0])
        raise ValueError(_format_snapshot_validation_error(source=source, error=error)) from exc
    identities: set[tuple[str, str]] = set()
    active_index: int | None = None
    for index, target in enumerate(snapshot.targets):
        provider, model = split_provider_model_reference(target.raw_model)
        if provider != target.provider or model != target.model:
            raise ValueError(format_invalid_provider_config_error(f"{source}.targets[{index}]", "must match its provider/model reference"))
        identity = (provider.casefold(), model.casefold())
        if identity in identities:
            raise ValueError(format_invalid_provider_config_error(f"{source}.targets", "must not contain duplicate provider targets"))
        identities.add(identity)
        if target == snapshot.active_target:
            active_index = index
    if active_index is None:
        raise ValueError(format_invalid_provider_config_error(f"{source}.active_target", "must reference one of the resolved provider targets"))
    targets = tuple(resolve_provider_model(target.raw_model, registry=registry) for target in snapshot.targets)
    first_raw_model = snapshot.targets[0].raw_model
    provider_fallback = (
        ProviderFallbackConfig(preferred_model=first_raw_model, fallback_models=tuple(target.raw_model for target in snapshot.targets[1:]))
        if len(targets) > 1
        else None
    )
    return ResolvedProviderConfig(
        model=first_raw_model,
        provider_fallback=provider_fallback,
        active_target=targets[active_index],
        target_chain=ResolvedProviderChain(preferred=targets[0], all_targets=targets),
    )


def _resolved_provider_target_snapshot(target: ResolvedProviderModel | BoundProviderModel) -> dict[str, str]:
    selection = target.selection
    if selection.raw_model is None or selection.provider is None or selection.model is None:
        raise ValueError("selected provider target must include its raw reference, provider and model")
    return {"raw_model": selection.raw_model, "provider": selection.provider, "model": selection.model}
