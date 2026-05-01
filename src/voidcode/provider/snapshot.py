from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .config import ProviderFallbackConfig
from .errors import format_invalid_provider_config_error
from .models import ResolvedProviderChain, ResolvedProviderConfig, ResolvedProviderModel
from .registry import ModelProviderRegistry
from .resolution import resolve_provider_model


class _ResolvedProviderTargetSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw_model: str
    provider: str
    model: str

    @field_validator("raw_model", "provider", "model", mode="before")
    @classmethod
    def _validate_required_string(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value


class _ResolvedProviderSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    active_target: _ResolvedProviderTargetSnapshotPayload
    targets: tuple[_ResolvedProviderTargetSnapshotPayload, ...]

    @field_validator("targets", mode="before")
    @classmethod
    def _validate_targets_array(cls, value: object) -> list[object]:
        if not isinstance(value, list):
            raise ValueError("must be an array")
        return cast(list[object], value)

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
    error_type = cast(str, error.get("type", ""))
    field_path = source
    for item in loc:
        if isinstance(item, int):
            field_path = f"{field_path}[{item}]"
            continue
        field_path = f"{field_path}.{item}"
    if error_type in {"model_type", "dict_type"}:
        return format_invalid_provider_config_error(field_path, "must be an object")
    reason = cast(str, error.get("msg", "is invalid"))
    if error_type == "value_error":
        context = error.get("ctx")
        if isinstance(context, dict):
            nested_error = cast(dict[str, object], context).get("error")
            if isinstance(nested_error, ValueError):
                reason = str(nested_error)
    return format_invalid_provider_config_error(field_path, reason)


def resolved_provider_snapshot(
    resolved_provider: ResolvedProviderConfig | Mapping[str, object] | None,
) -> dict[str, object] | None:
    if resolved_provider is None:
        return None
    if isinstance(resolved_provider, Mapping):
        provider_snapshot = cast(Mapping[str, object], resolved_provider)
        raw_active_target = provider_snapshot.get("active_target")
        raw_targets = provider_snapshot.get("targets")
        if not isinstance(raw_active_target, Mapping) or not isinstance(raw_targets, list):
            return None
        active_target = _snapshot_target_payload(cast(Mapping[str, object], raw_active_target))
        if active_target is None:
            return None
        normalized_targets: list[dict[str, str]] = []
        for item in cast(list[object], raw_targets):
            if not isinstance(item, Mapping):
                return None
            target = _snapshot_target_payload(cast(Mapping[str, object], item))
            if target is None:
                return None
            normalized_targets.append(target)
        if not normalized_targets:
            return None
        return {
            "active_target": active_target,
            "targets": normalized_targets,
        }

    targets: list[dict[str, str]] = []
    for target in resolved_provider.target_chain.all_targets:
        snapshot = _resolved_provider_target_snapshot(target)
        if snapshot is not None:
            targets.append(snapshot)
    if not targets:
        return None
    active_target = _resolved_provider_target_snapshot(resolved_provider.active_target)
    if active_target is None:
        return None
    return {
        "active_target": active_target,
        "targets": targets,
    }


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

    resolved_targets_list = [
        _resolved_provider_model_from_snapshot(
            item,
            source=f"{source}.targets[{index}]",
            registry=registry,
        )
        for index, item in enumerate(snapshot.targets)
    ]
    if not resolved_targets_list:
        raise ValueError(
            format_invalid_provider_config_error(f"{source}.targets", "must not be empty")
        )
    resolved_target_raw_models = [
        target.selection.raw_model for target in resolved_targets_list if target.selection.raw_model
    ]
    if len(set(resolved_target_raw_models)) != len(resolved_target_raw_models):
        raise ValueError(
            format_invalid_provider_config_error(
                f"{source}.targets",
                "must not contain duplicate provider targets",
            )
        )
    first_target = resolved_targets_list[0]
    resolved_targets: tuple[ResolvedProviderModel, ...] = tuple(resolved_targets_list)

    resolved_active_target = _resolved_provider_model_from_snapshot(
        snapshot.active_target,
        source=f"{source}.active_target",
        registry=registry,
    )
    resolved_target_raw_model_set = {
        target.selection.raw_model
        for target in resolved_targets
        if target.selection.raw_model is not None
    }
    if resolved_active_target.selection.raw_model not in resolved_target_raw_model_set:
        raise ValueError(
            format_invalid_provider_config_error(
                f"{source}.active_target",
                "must reference one of the resolved provider targets",
            )
        )

    provider_fallback: ProviderFallbackConfig | None = None
    if len(resolved_targets) > 1:
        first_raw_model = first_target.selection.raw_model
        if first_raw_model is None:
            raise ValueError(
                format_invalid_provider_config_error(
                    f"{source}.targets[0]",
                    "must include a raw_model",
                )
            )
        provider_fallback = ProviderFallbackConfig(
            preferred_model=first_raw_model,
            fallback_models=tuple(
                _require_raw_model(target=target, source=f"{source}.targets[{index}]")
                for index, target in enumerate(resolved_targets[1:], start=1)
            ),
        )

    first_raw_model = first_target.selection.raw_model
    if first_raw_model is None:
        raise ValueError(
            format_invalid_provider_config_error(
                f"{source}.targets[0]",
                "must include a raw_model",
            )
        )

    return ResolvedProviderConfig(
        model=first_raw_model,
        provider_fallback=provider_fallback,
        active_target=resolved_active_target,
        target_chain=ResolvedProviderChain(
            preferred=first_target,
            fallbacks=resolved_targets[1:],
            all_targets=resolved_targets,
        ),
    )


def _resolved_provider_target_snapshot(target: ResolvedProviderModel) -> dict[str, str] | None:
    if (
        target.selection.raw_model is None
        or target.selection.provider is None
        or target.selection.model is None
    ):
        return None
    return {
        "raw_model": target.selection.raw_model,
        "provider": target.selection.provider,
        "model": target.selection.model,
    }


def _snapshot_target_payload(payload: Mapping[str, object]) -> dict[str, str] | None:
    raw_model = payload.get("raw_model")
    provider = payload.get("provider")
    model = payload.get("model")
    if (
        not isinstance(raw_model, str)
        or not isinstance(provider, str)
        or not isinstance(model, str)
    ):
        return None
    return {
        "raw_model": raw_model,
        "provider": provider,
        "model": model,
    }


def _resolved_provider_model_from_snapshot(
    raw_value: object,
    *,
    source: str,
    registry: ModelProviderRegistry,
) -> ResolvedProviderModel:
    try:
        payload = _ResolvedProviderTargetSnapshotPayload.model_validate(raw_value)
    except ValidationError as exc:
        error = cast(dict[str, object], exc.errors(include_url=False)[0])
        raise ValueError(_format_snapshot_validation_error(source=source, error=error)) from exc

    resolved = resolve_provider_model(payload.raw_model, registry=registry)
    if resolved.selection.provider != payload.provider or resolved.selection.model != payload.model:
        raise ValueError(
            format_invalid_provider_config_error(
                source,
                "must match the parsed provider/model reference",
            )
        )
    return resolved


def _require_raw_model(*, target: ResolvedProviderModel, source: str) -> str:
    raw_model = target.selection.raw_model
    if raw_model is None:
        raise ValueError(format_invalid_provider_config_error(source, "must include a raw_model"))
    return raw_model
