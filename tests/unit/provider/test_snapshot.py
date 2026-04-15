from __future__ import annotations

import pytest

from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_config
from voidcode.provider.snapshot import parse_resolved_provider_snapshot, resolved_provider_snapshot
from voidcode.runtime.config import RuntimeProviderFallbackConfig


def test_resolved_provider_snapshot_round_trip_with_fallback_chain() -> None:
    registry = ModelProviderRegistry.with_defaults()
    resolved = resolve_provider_config(
        model="opencode/gpt-5.4",
        provider_fallback=RuntimeProviderFallbackConfig(
            preferred_model="opencode/gpt-5.4",
            fallback_models=("custom/demo",),
        ),
        registry=registry,
    )

    snapshot = resolved_provider_snapshot(resolved)

    assert snapshot is not None
    reparsed = parse_resolved_provider_snapshot(
        snapshot,
        source="persisted runtime_config.resolved_provider",
        registry=registry,
    )
    assert reparsed == resolved


def test_resolved_provider_snapshot_sanitizes_mapping_payload_to_minimal_shape() -> None:
    snapshot = resolved_provider_snapshot(
        {
            "active_target": {
                "raw_model": "opencode/gpt-5.4",
                "provider": "opencode",
                "model": "gpt-5.4",
                "api_key": "should-not-leak",
            },
            "targets": [
                {
                    "raw_model": "opencode/gpt-5.4",
                    "provider": "opencode",
                    "model": "gpt-5.4",
                    "token": "secret",
                },
                {
                    "raw_model": "custom/demo",
                    "provider": "custom",
                    "model": "demo",
                    "nested": {"secret": "nope"},
                },
            ],
            "provider_auth": {"api_key": "super-secret"},
        }
    )

    assert snapshot == {
        "active_target": {
            "raw_model": "opencode/gpt-5.4",
            "provider": "opencode",
            "model": "gpt-5.4",
        },
        "targets": [
            {
                "raw_model": "opencode/gpt-5.4",
                "provider": "opencode",
                "model": "gpt-5.4",
            },
            {
                "raw_model": "custom/demo",
                "provider": "custom",
                "model": "demo",
            },
        ],
    }


def test_parse_resolved_provider_snapshot_rejects_active_target_outside_target_chain() -> None:
    with pytest.raises(ValueError, match="must reference one of the resolved provider targets"):
        _ = parse_resolved_provider_snapshot(
            {
                "active_target": {
                    "raw_model": "custom/other",
                    "provider": "custom",
                    "model": "other",
                },
                "targets": [
                    {
                        "raw_model": "opencode/gpt-5.4",
                        "provider": "opencode",
                        "model": "gpt-5.4",
                    },
                    {
                        "raw_model": "custom/demo",
                        "provider": "custom",
                        "model": "demo",
                    },
                ],
            },
            source="persisted runtime_config.resolved_provider",
            registry=ModelProviderRegistry.with_defaults(),
        )


def test_parse_resolved_provider_snapshot_rejects_non_object_snapshot() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "invalid provider config: persisted runtime_config.resolved_provider must be an object"
        ),
    ):
        _ = parse_resolved_provider_snapshot(
            "not-an-object",
            source="persisted runtime_config.resolved_provider",
            registry=ModelProviderRegistry.with_defaults(),
        )


def test_parse_resolved_provider_snapshot_rejects_empty_targets() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "invalid provider config: "
            "persisted runtime_config.resolved_provider.targets must not be empty"
        ),
    ):
        _ = parse_resolved_provider_snapshot(
            {
                "active_target": {
                    "raw_model": "opencode/gpt-5.4",
                    "provider": "opencode",
                    "model": "gpt-5.4",
                },
                "targets": [],
            },
            source="persisted runtime_config.resolved_provider",
            registry=ModelProviderRegistry.with_defaults(),
        )
