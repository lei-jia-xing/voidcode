"""Provider naming contract: one canonical id and one human label per provider.

The canonical machine id is the lowercase vendor id (``minimax``); ``MiniMax`` is
the human label of that same provider. These tests pin the id/label split, the
case-insensitive input handling at every boundary, and the loud failure for an id
nothing declares.
"""

from __future__ import annotations

import pytest

from voidcode.provider.config import (
    OpenAICompatibleProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    parse_provider_configs_payload,
)
from voidcode.provider.naming import (
    BUILTIN_PROVIDER_IDS,
    UnknownProviderIdError,
    canonical_model_reference,
    canonical_provider_id,
    provider_label,
    split_provider_model_reference,
    unknown_provider_id_message,
)
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_config
from voidcode.runtime.provider_inspection import ProviderSummaryProjector, provider_credentials_config_path

_MINI_MAX_SPELLINGS = (
    "minimax/minimax-m2.5",
    "MiniMax/minimax-m2.5",
    "MINIMAX/minimax-m2.5",
    "  minimax /minimax-m2.5  ",
)


def _registered_minimax_registry() -> ModelProviderRegistry:
    return ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(minimax=OpenAICompatibleProviderConfig(api_key="sk-test")),
    )


def test_builtin_provider_ids_are_the_registry_keys() -> None:
    # One table of built-in provider ids: the naming authority and the registry
    # cannot drift apart without failing here.
    assert set(ModelProviderRegistry.with_defaults().providers) == set(BUILTIN_PROVIDER_IDS)


def test_every_builtin_provider_has_a_human_label() -> None:
    # No provider degrades to its raw id by omission from the label table.
    unlabelled = sorted(provider_id for provider_id in BUILTIN_PROVIDER_IDS if provider_label(provider_id) == provider_id)
    assert unlabelled == []


@pytest.mark.parametrize("provider_id", sorted(BUILTIN_PROVIDER_IDS))
def test_builtin_provider_ids_are_accepted_as_config_keys(provider_id: str) -> None:
    parsed = parse_provider_configs_payload(
        {provider_id: {}},
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert provider_id == "endpoint" or getattr(parsed, provider_id.replace("-", "_")) is not None


def test_canonical_provider_id_trims_and_lowercases() -> None:
    assert canonical_provider_id("MiniMax") == "minimax"
    assert canonical_provider_id(" MINIMAX  ") == "minimax"
    assert canonical_provider_id("opencode-go") == "opencode-go"


def test_provider_label_prefers_the_table_and_degrades_to_the_canonical_id() -> None:
    assert provider_label("minimax") == "MiniMax"
    assert provider_label("MiniMax") == "MiniMax"
    assert provider_label("openrouter") == "OpenRouter"
    assert provider_label("llama-local") == "llama-local"
    assert provider_label(" LLM-GW ") == "llm-gw"


def test_unknown_provider_id_message_names_canonical_ids_and_the_custom_path() -> None:
    message = unknown_provider_id_message(" MiniMaxx ")

    assert "unknown provider id 'minimaxx'" in message
    assert "known provider ids are" in message
    for provider_id in sorted(BUILTIN_PROVIDER_IDS):
        assert provider_id in message
    assert "providers.custom.minimaxx" in message
    assert "minimaxx/<model>" in message


def test_unknown_provider_id_error_is_a_value_error() -> None:
    # The CLI and HTTP boundaries translate ValueError into a user-facing error.
    error = UnknownProviderIdError("ghost")

    assert isinstance(error, ValueError)


@pytest.mark.parametrize("raw_model", _MINI_MAX_SPELLINGS)
def test_provider_spelling_variants_resolve_to_the_same_provider(raw_model: str) -> None:
    registry = _registered_minimax_registry()

    resolved = resolve_provider_config(raw_model, None, registry=registry)
    reference = resolve_provider_config(_MINI_MAX_SPELLINGS[0], None, registry=registry)

    assert resolved.active_target.selection.provider == "minimax"
    assert resolved.active_target.selection.model == "minimax-m2.5"
    # Same provider, same resolution source, same capability metadata. Only the
    # spelling the user typed differs, and that is asserted separately.
    assert resolved.active_target.selection.provider == reference.active_target.selection.provider
    assert resolved.active_target.selection.model == reference.active_target.selection.model
    assert resolved.active_target.provider is reference.active_target.provider
    assert resolved.active_target.resolution == reference.active_target.resolution
    assert resolved.active_target.metadata == reference.active_target.metadata


@pytest.mark.parametrize("raw_model", _MINI_MAX_SPELLINGS)
def test_provider_spelling_variants_keep_the_spelling_as_the_raw_reference(raw_model: str) -> None:
    resolved = resolve_provider_config(raw_model, None, registry=_registered_minimax_registry())

    assert resolved.model == raw_model
    assert resolved.active_target.selection.raw_model == raw_model


@pytest.mark.parametrize("raw_model", ["minimaxx/minimax-m2.5", "min-max/minimax-m2.5"])
def test_undeclared_provider_id_fails_loudly(raw_model: str) -> None:
    with pytest.raises(UnknownProviderIdError) as excinfo:
        _ = resolve_provider_config(raw_model, None, registry=ModelProviderRegistry.with_defaults())

    message = str(excinfo.value)
    assert "unknown provider id" in message
    assert "known provider ids are" in message
    assert "providers.custom." in message


def test_declared_custom_provider_resolves_under_any_spelling() -> None:
    declared = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(custom={"local-gw": ProviderEndpointConfig(base_url="http://localhost:11434/v1")}),
    )

    resolved = resolve_provider_config("Local-GW/coder", None, registry=declared)

    assert resolved.active_target.selection.provider == "local-gw"
    assert resolved.active_target.resolution.source == "custom"
    assert resolved.active_target.resolution.configured is True


def test_provider_summary_pairs_the_canonical_id_with_the_label() -> None:
    # The web `/api/providers` payload and `voidcode provider inspect` both read
    # this pair from the projector.
    summary = ProviderSummaryProjector.project_one(
        "minimax",
        current_provider="minimax",
        label_for=provider_label,
        is_configured=lambda _name: True,
    )

    assert (summary.name, summary.label) == ("minimax", "MiniMax")
    assert summary.current is True
    assert summary.configured is True


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        # Top-level `api_key` shapes: openai, anthropic, named endpoints and
        # every shared OpenAI-compatible vendor.
        ("openai", "providers.openai.api_key"),
        ("anthropic", "providers.anthropic.api_key"),
        ("endpoint", "providers.endpoint.api_key"),
        ("opencode-zen", "providers.opencode-zen.api_key"),
        ("kimi-code", "providers.kimi-code.api_key"),
        ("minimax", "providers.minimax.api_key"),
        ("minimax-cn", "providers.minimax-cn.api_key"),
        # Nested `auth.*` shapes: google reads `auth.api_key`, copilot `auth.token`.
        ("google", "providers.google.auth.api_key"),
        ("github-copilot", "providers.github-copilot.auth.token"),
        # A declared custom provider is endpoint-shaped: top-level `api_key`.
        ("local-gw", "providers.custom.local-gw.api_key"),
    ],
)
def test_credentials_config_path_names_the_key_a_user_writes(provider_id: str, expected: str) -> None:
    # Remediation text must name the spelling the config file actually accepts:
    # the payload key (not the Python field name) and the credential leaf the
    # provider's own shape reads (top-level `api_key`, or `auth.api_key` /
    # `auth.token` for the nested shapes).
    assert provider_credentials_config_path(provider_id) == expected


def test_provider_summary_label_degrades_for_an_unlabelled_custom_provider() -> None:
    summary = ProviderSummaryProjector.project_one(
        "local-gw",
        current_provider=None,
        label_for=provider_label,
        is_configured=lambda _name: False,
    )

    assert (summary.name, summary.label) == ("local-gw", "local-gw")


@pytest.mark.parametrize(
    ("raw_model", "expected"),
    [
        ("minimax/minimax-m2.5", ("minimax", "minimax-m2.5")),
        ("MiniMax/MiniMax-M2.5", ("minimax", "MiniMax-M2.5")),
        ("  MiniMax / MiniMax-M2.5 ", ("minimax", "MiniMax-M2.5")),
        ("endpoint/openrouter/openai/gpt-4o", ("endpoint", "openrouter/openai/gpt-4o")),
    ],
)
def test_split_provider_model_reference_canonicalises_only_the_provider(
    raw_model: str,
    expected: tuple[str, str],
) -> None:
    assert split_provider_model_reference(raw_model) == expected


@pytest.mark.parametrize("raw_model", ["", "provider", "/model", "provider/", " /model", "provider/  "])
def test_split_provider_model_reference_rejects_malformed_references(raw_model: str) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        _ = split_provider_model_reference(raw_model)


def test_canonical_model_reference_keeps_the_model_case() -> None:
    assert canonical_model_reference(" MiniMax / MiniMax-M2.5 ") == "minimax/MiniMax-M2.5"
