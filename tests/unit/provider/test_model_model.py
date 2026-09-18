from __future__ import annotations

import pytest

from voidcode.provider.config import ProviderConfigs, ProviderEndpointConfig
from voidcode.provider.models import ResolvedProviderConfig
from voidcode.provider.naming import UnknownProviderIdError
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import (
    resolve_provider_chain,
    resolve_provider_config,
    resolve_provider_model,
)
from voidcode.runtime.config import RuntimeProviderFallbackConfig


def _registry_with_custom(*provider_names: str) -> ModelProviderRegistry:
    """Default registry plus the named providers declared under ``providers.custom``."""
    return ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(
            custom={provider_name: ProviderEndpointConfig(base_url="http://localhost:11434/v1") for provider_name in provider_names}
        )
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


def test_resolve_provider_model_allows_slashes_inside_model_id() -> None:
    resolved = resolve_provider_model(
        "endpoint/openrouter/openai/gpt-4o",
        registry=ModelProviderRegistry.with_defaults(),
    )

    assert resolved.selection.provider == "endpoint"
    assert resolved.selection.model == "openrouter/openai/gpt-4o"
    assert resolved.selection.raw_model == "endpoint/openrouter/openai/gpt-4o"
    assert resolved.provider is not None
    assert resolved.provider.name == "endpoint"
    assert resolved.resolution.source == "builtin"
    assert resolved.resolution.configured is True


def test_resolve_provider_model_rejects_unknown_provider_name() -> None:
    # An id that is neither built-in nor declared must not degrade into the
    # generic endpoint provider: the error names the canonical ids and the way to
    # declare a custom OpenAI-compatible endpoint.
    with pytest.raises(UnknownProviderIdError) as excinfo:
        _ = resolve_provider_model(
            "demo-provider/demo-model",
            registry=ModelProviderRegistry.with_defaults(),
        )

    message = str(excinfo.value)
    assert "unknown provider id 'demo-provider'" in message
    assert "known provider ids are" in message
    assert "minimax" in message
    assert "providers.custom.demo-provider" in message


def test_resolve_provider_model_canonicalises_provider_segment_case() -> None:
    registry = ModelProviderRegistry.with_defaults()

    canonical = resolve_provider_model("minimax/MiniMax-M2.5", registry=registry)
    variant = resolve_provider_model("MiniMax/MiniMax-M2.5", registry=registry)

    assert canonical.selection.provider == "minimax"
    assert variant.selection.provider == "minimax"
    assert variant.selection.model == canonical.selection.model == "MiniMax-M2.5"
    # The spelling the user typed stays the reference; only the parsed id changes.
    assert variant.selection.raw_model == "MiniMax/MiniMax-M2.5"
    assert variant.resolution == canonical.resolution
    assert variant.metadata == canonical.metadata
    assert variant.provider is canonical.provider


@pytest.mark.parametrize(
    "raw_model",
    ["minimax/MiniMax-M2.5", "MiniMax/MiniMax-M2.5", "MINIMAX/MiniMax-M2.5", "  minimax /MiniMax-M2.5  "],
)
def test_resolve_provider_model_spelling_variants_resolve_identically(raw_model: str) -> None:
    resolved = resolve_provider_model(raw_model, registry=ModelProviderRegistry.with_defaults())

    assert resolved.selection.provider == "minimax"
    # The wire value keeps the vendor's own casing and is only trimmed.
    assert resolved.selection.model == "MiniMax-M2.5"
    assert resolved.resolution.source == "builtin"
    assert resolved.resolution.configured is True
    assert resolved.provider is not None
    assert resolved.provider.name == "minimax"
    assert resolved.metadata is not None
    assert resolved.metadata.context_window is not None


def test_resolve_provider_model_marks_custom_configured_provider_resolution() -> None:
    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(custom={"llama-local": ProviderEndpointConfig(base_url="http://localhost:11434/v1")})
    )

    resolved = resolve_provider_model("llama-local/coder", registry=registry)

    assert resolved.provider is not None
    assert resolved.provider.name == "llama-local"
    assert resolved.resolution.source == "custom"
    assert resolved.resolution.configured is True


def test_resolve_provider_chain_preserves_ordered_fallback_targets() -> None:
    resolved = resolve_provider_chain(
        RuntimeProviderFallbackConfig(
            preferred_model="opencode/gpt-5.4",
            fallback_models=("opencode/gpt-5.3", "llama-local/demo"),
        ),
        registry=_registry_with_custom("llama-local"),
    )

    assert resolved.preferred.selection.raw_model == "opencode/gpt-5.4"
    assert [target.selection.raw_model for target in resolved.fallbacks] == [
        "opencode/gpt-5.3",
        "llama-local/demo",
    ]
    assert [target.selection.raw_model for target in resolved.all_targets] == [
        "opencode/gpt-5.4",
        "opencode/gpt-5.3",
        "llama-local/demo",
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
    assert [target.selection.raw_model for target in resolved.target_chain.all_targets] == ["opencode/gpt-5.4"]


def test_resolve_provider_config_normalizes_preferred_fallback_model_as_active_target() -> None:
    resolved = resolve_provider_config(
        model="opencode/gpt-5.4",
        provider_fallback=RuntimeProviderFallbackConfig(
            preferred_model="opencode/gpt-5.4",
            fallback_models=("llama-local/demo",),
        ),
        registry=_registry_with_custom("llama-local"),
    )

    assert resolved.model == "opencode/gpt-5.4"
    assert resolved.active_target.selection.raw_model == "opencode/gpt-5.4"
    assert [target.selection.raw_model for target in resolved.target_chain.all_targets] == [
        "opencode/gpt-5.4",
        "llama-local/demo",
    ]


def test_resolve_provider_config_rewrites_preferred_fallback_to_match_explicit_model() -> None:
    resolved = resolve_provider_config(
        model="opencode/gpt-5.4",
        provider_fallback=RuntimeProviderFallbackConfig(
            preferred_model="llama-local/demo",
            fallback_models=("backup/model", "opencode/gpt-5.4"),
        ),
        registry=_registry_with_custom("llama-local", "backup"),
    )

    assert resolved.model == "opencode/gpt-5.4"
    assert resolved.provider_fallback == RuntimeProviderFallbackConfig(
        preferred_model="opencode/gpt-5.4",
        fallback_models=("backup/model",),
    )
    assert [target.selection.raw_model for target in resolved.target_chain.all_targets] == [
        "opencode/gpt-5.4",
        "backup/model",
    ]


def test_resolve_provider_chain_rejects_duplicate_targets_even_without_parser() -> None:
    with pytest.raises(ValueError, match="duplicate models"):
        _ = resolve_provider_chain(
            RuntimeProviderFallbackConfig(
                preferred_model="opencode/gpt-5.4",
                fallback_models=("opencode/gpt-5.4",),
            ),
            registry=ModelProviderRegistry.with_defaults(),
        )


@pytest.mark.parametrize("raw_model", ["", "provider", "/model", "provider/", "  /model", "provider/   "])
def test_resolve_provider_model_rejects_malformed_reference(raw_model: str) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        _ = resolve_provider_model(raw_model, registry=ModelProviderRegistry.with_defaults())


def test_resolve_provider_chain_compares_targets_case_insensitively() -> None:
    # `MiniMax/m2.5` and `minimax/M2.5` are the same target: one provider, one
    # model id, so the chain must reject the duplicate.
    with pytest.raises(ValueError, match="duplicate models"):
        _ = resolve_provider_chain(
            RuntimeProviderFallbackConfig(
                preferred_model="minimax/MiniMax-M2.5",
                fallback_models=("MiniMax/minimax-m2.5",),
            ),
            registry=ModelProviderRegistry.with_defaults(),
        )
