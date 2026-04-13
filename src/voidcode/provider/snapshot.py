from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from .config import ProviderFallbackConfig
from .errors import format_invalid_provider_config_error
from .models import ResolvedProviderChain, ResolvedProviderConfig, ResolvedProviderModel
from .registry import ModelProviderRegistry
from .resolution import resolve_provider_model


def resolved_provider_snapshot(
    resolved_provider: ResolvedProviderConfig | Mapping[str, object] | None,
) -> dict[str, object] | None:
    if resolved_provider is None:
        return None
    if isinstance(resolved_provider, Mapping):
        raw_active_target = resolved_provider.get("active_target")
        raw_targets = resolved_provider.get("targets")
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
    if not isinstance(raw_snapshot, dict):
        raise ValueError(format_invalid_provider_config_error(source, "must be an object"))

    snapshot = cast(dict[str, object], raw_snapshot)
    raw_active_target = snapshot.get("active_target")
    raw_targets = snapshot.get("targets")
    if not isinstance(raw_active_target, dict):
        raise ValueError(
            format_invalid_provider_config_error(f"{source}.active_target", "must be an object")
        )
    if not isinstance(raw_targets, list):
        raise ValueError(
            format_invalid_provider_config_error(f"{source}.targets", "must be an array")
        )

    raw_targets_list = cast(list[object], raw_targets)
    resolved_targets_list = [
        _resolved_provider_model_from_snapshot(
            item,
            source=f"{source}.targets[{index}]",
            registry=registry,
        )
        for index, item in enumerate(raw_targets_list)
    ]
    if not resolved_targets_list:
        raise ValueError(
            format_invalid_provider_config_error(f"{source}.targets", "must not be empty")
        )
    first_target = resolved_targets_list[0]
    resolved_targets: tuple[ResolvedProviderModel, ...] = tuple(resolved_targets_list)

    resolved_active_target = _resolved_provider_model_from_snapshot(
        cast(object, raw_active_target),
        source=f"{source}.active_target",
        registry=registry,
    )
    resolved_target_raw_models = {
        target.selection.raw_model
        for target in resolved_targets
        if target.selection.raw_model is not None
    }
    if resolved_active_target.selection.raw_model not in resolved_target_raw_models:
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
    if not isinstance(raw_value, dict):
        raise ValueError(format_invalid_provider_config_error(source, "must be an object"))
    payload = cast(dict[str, object], raw_value)
    raw_model = payload.get("raw_model")
    provider = payload.get("provider")
    model = payload.get("model")
    if (
        not isinstance(raw_model, str)
        or not isinstance(provider, str)
        or not isinstance(model, str)
    ):
        raise ValueError(
            format_invalid_provider_config_error(
                source,
                "must include string raw_model, provider, and model fields",
            )
        )

    resolved = resolve_provider_model(raw_model, registry=registry)
    if resolved.selection.provider != provider or resolved.selection.model != model:
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
