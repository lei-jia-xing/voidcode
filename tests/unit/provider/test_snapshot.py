from __future__ import annotations

from copy import deepcopy

import pytest

from voidcode.provider.config import ProviderConfigs, ProviderEndpointConfig, ProviderFallbackConfig
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_config
from voidcode.provider.snapshot import parse_resolved_provider_snapshot, resolved_provider_snapshot


def test_resolved_provider_snapshot_round_trip_preserves_active_fallback_and_model_case() -> None:
    from dataclasses import replace

    registry = ModelProviderRegistry.with_defaults(
        provider_configs=ProviderConfigs(custom={"local": ProviderEndpointConfig(base_url="http://127.0.0.1:11434/v1")})
    )
    resolved = resolve_provider_config(
        "OpenAI/GPT-5.4",
        ProviderFallbackConfig(preferred_model="OpenAI/GPT-5.4", fallback_models=("local/VendorCase",)),
        registry=registry,
    )
    resolved = replace(resolved, active_target=resolved.target_chain.all_targets[1])
    snapshot = resolved_provider_snapshot(resolved)
    reparsed = parse_resolved_provider_snapshot(snapshot, source="recorded provider", registry=registry)
    assert reparsed == resolved
    assert tuple(target.selection.model for target in reparsed.target_chain.all_targets) == ("GPT-5.4", "VendorCase")
    assert reparsed.active_target.selection.provider == "local"


@pytest.mark.parametrize("version", [None, True, False, 1, 3, 2.0, "2"])
def test_provider_snapshot_refuses_unsupported_or_noninteger_version(version: object) -> None:
    registry = ModelProviderRegistry.with_defaults()
    snapshot = resolved_provider_snapshot(resolve_provider_config("openai/gpt-5.4", None, registry=registry))
    assert snapshot is not None
    snapshot["schema_version"] = version
    with pytest.raises(ValueError):
        parse_resolved_provider_snapshot(snapshot, source="recorded provider", registry=registry)


@pytest.mark.parametrize("damage", ["missing-version", "unknown-root", "unknown-target", "empty-chain", "wrong-model", "outside-chain", "duplicate"])
def test_provider_snapshot_refuses_noncanonical_closed_target_state(damage: str) -> None:
    registry = ModelProviderRegistry.with_defaults()
    snapshot = resolved_provider_snapshot(resolve_provider_config("OpenAI/GPT-5.4", None, registry=registry))
    assert snapshot is not None
    payload = deepcopy(snapshot)
    target = {"raw_model": "OpenAI/GPT-5.4", "provider": "openai", "model": "GPT-5.4"}
    match damage:
        case "missing-version":
            del payload["schema_version"]
        case "unknown-root":
            payload["current_provider"] = "openai"
        case "unknown-target":
            payload["targets"] = [{**target, "configured": True}]
        case "empty-chain":
            payload["targets"] = []
        case "wrong-model":
            payload["targets"] = [{**target, "model": "different"}]
        case "outside-chain":
            payload["active_target"] = {"raw_model": "openai/other", "provider": "openai", "model": "other"}
        case "duplicate":
            payload["targets"] = [target, {"raw_model": "openai/gpt-5.4", "provider": "openai", "model": "gpt-5.4"}]
    with pytest.raises(ValueError):
        parse_resolved_provider_snapshot(payload, source="recorded provider", registry=registry)


def test_provider_snapshot_refuses_undeclared_provider_without_endpoint_fallback() -> None:
    target = {"raw_model": "ghost/model", "provider": "ghost", "model": "model"}
    with pytest.raises(ValueError):
        parse_resolved_provider_snapshot(
            {"schema_version": 2, "active_target": target, "targets": [target]},
            source="recorded provider",
            registry=ModelProviderRegistry.with_defaults(),
        )
