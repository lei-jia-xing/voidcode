from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.model_provider import ModelProviderRegistry, resolve_provider_model


def test_resolve_provider_model_accepts_none() -> None:
    resolved = resolve_provider_model(None, registry=ModelProviderRegistry.with_defaults())

    assert resolved.selection.raw_model is None
    assert resolved.selection.provider is None
    assert resolved.selection.model is None
    assert resolved.provider is None


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
    assert resolved.provider.capabilities.model_reference_formats == ("provider/model",)


def test_resolve_provider_model_creates_generic_provider_for_unknown_name() -> None:
    resolved = resolve_provider_model(
        "custom/demo-model",
        registry=ModelProviderRegistry.with_defaults(),
    )

    assert resolved.selection.provider == "custom"
    assert resolved.selection.model == "demo-model"
    assert resolved.provider is not None
    assert resolved.provider.name == "custom"


@pytest.mark.parametrize("raw_model", ["", "provider", "/model", "provider/", "a/b/c"])
def test_resolve_provider_model_rejects_malformed_reference(raw_model: str) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        _ = resolve_provider_model(raw_model, registry=ModelProviderRegistry.with_defaults())
