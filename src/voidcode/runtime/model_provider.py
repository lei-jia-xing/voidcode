from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from .config import RuntimeProviderFallbackConfig
from .provider_errors import format_invalid_provider_config_error
from .single_agent_provider import SingleAgentProvider, StubSingleAgentProvider


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def single_agent_provider(self) -> SingleAgentProvider: ...


@dataclass(frozen=True, slots=True)
class StaticModelProvider:
    name: str

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)


@dataclass(frozen=True, slots=True)
class ProviderModelSelection:
    raw_model: str | None = None
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProviderModel:
    selection: ProviderModelSelection = ProviderModelSelection()
    provider: ModelProvider | None = None


@dataclass(frozen=True, slots=True)
class ResolvedProviderChain:
    preferred: ResolvedProviderModel = ResolvedProviderModel()
    fallbacks: tuple[ResolvedProviderModel, ...] = ()
    all_targets: tuple[ResolvedProviderModel, ...] = ()

    def target_at(self, index: int) -> ResolvedProviderModel | None:
        if index < 0 or index >= len(self.all_targets):
            return None
        return self.all_targets[index]


@dataclass(frozen=True, slots=True)
class ResolvedProviderConfig:
    model: str | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    active_target: ResolvedProviderModel = ResolvedProviderModel()
    target_chain: ResolvedProviderChain = ResolvedProviderChain()


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelProvider]

    @classmethod
    def with_defaults(cls) -> ModelProviderRegistry:
        return cls(providers={"opencode": StaticModelProvider(name="opencode")})

    def resolve(self, provider_name: str) -> ModelProvider:
        return self.providers.get(provider_name, StaticModelProvider(name=provider_name))


def resolve_provider_model(
    raw_model: str | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderModel:
    if raw_model is None:
        return ResolvedProviderModel()

    provider_name, model_name = _parse_model_reference(raw_model)
    provider = registry.resolve(provider_name)
    return ResolvedProviderModel(
        selection=ProviderModelSelection(
            raw_model=raw_model,
            provider=provider_name,
            model=model_name,
        ),
        provider=provider,
    )


def resolve_provider_chain(
    provider_fallback: RuntimeProviderFallbackConfig | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderChain:
    if provider_fallback is None:
        return ResolvedProviderChain()

    preferred = resolve_provider_model(provider_fallback.preferred_model, registry=registry)
    fallbacks = tuple(
        resolve_provider_model(raw_model, registry=registry)
        for raw_model in provider_fallback.fallback_models
    )
    return ResolvedProviderChain(
        preferred=preferred,
        fallbacks=fallbacks,
        all_targets=(preferred, *fallbacks),
    )


def resolve_provider_config(
    model: str | None,
    provider_fallback: RuntimeProviderFallbackConfig | None,
    *,
    registry: ModelProviderRegistry,
) -> ResolvedProviderConfig:
    if provider_fallback is not None:
        if model is not None and model != provider_fallback.preferred_model:
            raise ValueError(
                format_invalid_provider_config_error(
                    "provider_fallback.preferred_model",
                    "must match model when both are configured",
                )
            )
        target_chain = resolve_provider_chain(provider_fallback, registry=registry)
        return ResolvedProviderConfig(
            model=provider_fallback.preferred_model,
            provider_fallback=provider_fallback,
            active_target=target_chain.preferred,
            target_chain=target_chain,
        )

    if model is None:
        return ResolvedProviderConfig()

    active_target = resolve_provider_model(model, registry=registry)
    target_chain = ResolvedProviderChain(
        preferred=active_target,
        all_targets=(active_target,),
    )
    return ResolvedProviderConfig(
        model=model,
        provider_fallback=None,
        active_target=active_target,
        target_chain=target_chain,
    )


def resolved_provider_snapshot(
    resolved_provider: ResolvedProviderConfig | Mapping[str, object] | None,
) -> dict[str, object] | None:
    if resolved_provider is None:
        return None
    if isinstance(resolved_provider, Mapping):
        return dict(resolved_provider)

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
    if resolved_active_target.selection.raw_model != first_target.selection.raw_model:
        raise ValueError(
            format_invalid_provider_config_error(
                f"{source}.active_target",
                "must match the first resolved provider target",
            )
        )

    provider_fallback: RuntimeProviderFallbackConfig | None = None
    if len(resolved_targets) > 1:
        first_raw_model = first_target.selection.raw_model
        if first_raw_model is None:
            raise ValueError(
                format_invalid_provider_config_error(
                    f"{source}.targets[0]",
                    "must include a raw_model",
                )
            )
        provider_fallback = RuntimeProviderFallbackConfig(
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


def _parse_model_reference(raw_model: str) -> tuple[str, str]:
    provider_name, separator, model_name = raw_model.partition("/")
    if separator != "/" or "/" in model_name or not provider_name or not model_name:
        raise ValueError("model must use provider/model format")
    return provider_name, model_name


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
