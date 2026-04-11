from __future__ import annotations

import pytest

from voidcode.runtime.config import RuntimeProviderFallbackConfig
from voidcode.runtime.model_provider import (
    ModelProviderRegistry,
    resolve_provider_chain,
    resolve_provider_model,
)


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


@pytest.mark.parametrize("raw_model", ["", "provider", "/model", "provider/", "a/b/c"])
def test_resolve_provider_model_rejects_malformed_reference(raw_model: str) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        _ = resolve_provider_model(raw_model, registry=ModelProviderRegistry.with_defaults())
