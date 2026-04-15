from __future__ import annotations

import pytest

from voidcode.provider.litellm import LiteLLMModelProvider
from voidcode.provider.models import ResolvedProviderConfig
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import (
    resolve_provider_chain,
    resolve_provider_config,
    resolve_provider_model,
)
from voidcode.runtime.config import RuntimeProviderFallbackConfig


def test_resolve_provider_model_accepts_none() -> None:
    resolved = resolve_provider_model(None, registry=ModelProviderRegistry.with_defaults())

    assert resolved.selection.raw_model is None
    assert resolved.selection.provider is None
    assert resolved.selection.model is None
    assert resolved.provider is None


def test_resolve_provider_chain_accepts_none() -> None:
    resolved = resolve_provider_chain(None, registry=ModelProviderRegistry.with_defaults())

    assert resolved.preferred.selection.raw_model is None
    assert resolved.fallbacks == ()
    assert resolved.all_targets == ()


def test_resolve_provider_model_parses_known_provider_reference() -> None:
    resolved = resolve_provider_model(
        "opencode/gpt-5.4",
        registry=ModelProviderRegistry.with_defaults(),
    )

    assert resolved.selection.raw_model == "opencode/gpt-5.4"
    assert resolved.selection.provider == "opencode"
    assert resolved.selection.model == "gpt-5.4"
    assert resolved.provider is not None
    assert resolved.provider.name == "opencode"


def test_resolve_provider_model_creates_generic_provider_for_unknown_name() -> None:
    resolved = resolve_provider_model(
        "custom/demo-model",
        registry=ModelProviderRegistry.with_defaults(),
    )

    assert resolved.selection.provider == "custom"
    assert resolved.selection.model == "demo-model"
    assert resolved.provider is not None
    assert resolved.provider.name == "custom"
    assert isinstance(resolved.provider, LiteLLMModelProvider)


def test_resolve_provider_chain_preserves_ordered_fallback_targets() -> None:
    resolved = resolve_provider_chain(
        RuntimeProviderFallbackConfig(
            preferred_model="opencode/gpt-5.4",
            fallback_models=("opencode/gpt-5.3", "custom/demo"),
        ),
        registry=ModelProviderRegistry.with_defaults(),
    )

    assert resolved.preferred.selection.raw_model == "opencode/gpt-5.4"
    assert [target.selection.raw_model for target in resolved.fallbacks] == [
        "opencode/gpt-5.3",
        "custom/demo",
    ]
    assert [target.selection.raw_model for target in resolved.all_targets] == [
        "opencode/gpt-5.4",
        "opencode/gpt-5.3",
        "custom/demo",
    ]


def test_resolve_provider_config_builds_single_target_chain_from_model() -> None:
    resolved = resolve_provider_config(
        model="opencode/gpt-5.4",
        provider_fallback=None,
        registry=ModelProviderRegistry.with_defaults(),
    )

    assert resolved == ResolvedProviderConfig(
        model="opencode/gpt-5.4",
        provider_fallback=None,
        active_target=resolved.active_target,
        target_chain=resolved.target_chain,
    )
    assert resolved.active_target.selection.raw_model == "opencode/gpt-5.4"
    assert [target.selection.raw_model for target in resolved.target_chain.all_targets] == [
        "opencode/gpt-5.4"
    ]


def test_resolve_provider_config_normalizes_preferred_fallback_model_as_active_target() -> None:
    resolved = resolve_provider_config(
        model="opencode/gpt-5.4",
        provider_fallback=RuntimeProviderFallbackConfig(
            preferred_model="opencode/gpt-5.4",
            fallback_models=("custom/demo",),
        ),
        registry=ModelProviderRegistry.with_defaults(),
    )

    assert resolved.model == "opencode/gpt-5.4"
    assert resolved.active_target.selection.raw_model == "opencode/gpt-5.4"
    assert [target.selection.raw_model for target in resolved.target_chain.all_targets] == [
        "opencode/gpt-5.4",
        "custom/demo",
    ]


def test_resolve_provider_config_rejects_mismatched_model_and_preferred_fallback() -> None:
    with pytest.raises(ValueError, match="preferred_model"):
        _ = resolve_provider_config(
            model="opencode/gpt-5.4",
            provider_fallback=RuntimeProviderFallbackConfig(
                preferred_model="custom/demo",
                fallback_models=("backup/model",),
            ),
            registry=ModelProviderRegistry.with_defaults(),
        )


@pytest.mark.parametrize("raw_model", ["", "provider", "/model", "provider/", "a/b/c"])
def test_resolve_provider_model_rejects_malformed_reference(raw_model: str) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        _ = resolve_provider_model(raw_model, registry=ModelProviderRegistry.with_defaults())
