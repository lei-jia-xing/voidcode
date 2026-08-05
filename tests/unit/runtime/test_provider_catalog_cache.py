from __future__ import annotations

import json
from pathlib import Path

from voidcode.provider.model_catalog import ProviderModelCatalog, ProviderModelMetadata
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.runtime.provider_catalog_cache import RuntimeProviderCatalogCache


def test_provider_catalog_cache_round_trips_catalog_metadata(tmp_path: Path) -> None:
    cache_path = tmp_path / "provider-model-catalog.json"
    source_registry = ModelProviderRegistry(
        providers={},
        model_catalog={
            "example": ProviderModelCatalog(
                provider="example",
                models=("model-a",),
                refreshed=True,
                model_metadata={
                    "model-a": ProviderModelMetadata(
                        context_window=32_000,
                        supports_tools=True,
                        modalities_input=("text", "image"),
                    )
                },
                source="remote",
                last_refresh_status="ok",
                discovery_mode="configured_endpoint",
            )
        },
    )

    RuntimeProviderCatalogCache(registry=source_registry, path=cache_path).persist()
    target_registry = ModelProviderRegistry(providers={}, model_catalog={})
    RuntimeProviderCatalogCache(registry=target_registry, path=cache_path).hydrate()

    restored = target_registry.provider_catalog("example")
    assert restored is not None
    assert restored.models == ("model-a",)
    assert restored.model_metadata["model-a"].context_window == 32_000
    assert restored.model_metadata["model-a"].supports_tools is True
    assert restored.model_metadata["model-a"].modalities_input == ("text", "image")


def test_provider_catalog_cache_ignores_invalid_entries_and_preserves_valid_ones(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "provider-model-catalog.json"
    cache_path.write_text(
        json.dumps(
            {
                "providers": {
                    "invalid/name": {"models": ["ignored"]},
                    "invalid-shape": {"models": "not-a-list"},
                    "valid": {
                        "models": ["model-a", "", 7],
                        "model_metadata": {"model-a": {"context_window": 8_192}},
                        "discovery_mode": "unknown-mode",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    registry = ModelProviderRegistry(providers={}, model_catalog={})

    RuntimeProviderCatalogCache(registry=registry, path=cache_path).hydrate()

    assert tuple(registry.model_catalog or {}) == ("valid",)
    restored = registry.provider_catalog("valid")
    assert restored is not None
    assert restored.models == ("model-a",)
    assert restored.discovery_mode == "unavailable"


def test_provider_catalog_cache_does_not_replace_live_catalog(tmp_path: Path) -> None:
    cache_path = tmp_path / "provider-model-catalog.json"
    cache_path.write_text(
        json.dumps({"providers": {"cached": {"models": ["cached-model"]}}}),
        encoding="utf-8",
    )
    live = ProviderModelCatalog(provider="live", models=("live-model",), refreshed=False)
    registry = ModelProviderRegistry(providers={}, model_catalog={"live": live})

    RuntimeProviderCatalogCache(registry=registry, path=cache_path).hydrate()

    assert registry.model_catalog == {"live": live}
