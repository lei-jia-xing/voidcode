from __future__ import annotations

import pytest

from voidcode.provider.model_catalog import ProviderModelCatalog, ProviderModelMetadata
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.runtime.provider_catalog_query import RuntimeProviderCatalogQuery


def _query() -> RuntimeProviderCatalogQuery:
    registry = ModelProviderRegistry(
        providers={},
        model_catalog={
            "openai": ProviderModelCatalog(
                provider="openai",
                models=("gpt-4o",),
                refreshed=True,
                model_metadata={"gpt-4o": ProviderModelMetadata(context_window=64_000)},
                source="remote",
                last_refresh_status="ok",
                discovery_mode="configured_endpoint",
            )
        },
    )
    return RuntimeProviderCatalogQuery(registry=registry)


def test_provider_catalog_query_projects_catalog_without_runtime_state() -> None:
    query = _query()

    assert query.models("openai") == ("gpt-4o",)
    assert query.catalog_payload("openai") == {
        "provider": "openai",
        "models": ["gpt-4o"],
        "model_metadata": {"gpt-4o": {"context_window": 64_000, "max_input_tokens": 64_000}},
        "refreshed": True,
        "source": "remote",
        "last_refresh_status": "ok",
        "last_error": None,
        "discovery_mode": "configured_endpoint",
    }


def test_provider_catalog_query_merges_catalog_override_with_inferred_metadata() -> None:
    metadata = _query().metadata_for_model("openai", "gpt-4o")

    assert metadata is not None
    assert metadata.context_window == 64_000
    assert metadata.supports_tools is True
    assert metadata.supports_vision is True


def test_provider_catalog_query_falls_back_to_inferred_metadata() -> None:
    registry = ModelProviderRegistry(providers={}, model_catalog={})
    query = RuntimeProviderCatalogQuery(registry=registry)

    metadata = query.metadata_for_model("openai", "gpt-4o")

    assert metadata is not None
    assert metadata.context_window is not None


def test_provider_catalog_query_builds_models_result_from_catalog() -> None:
    result = _query().models_result("openai", configured=True)

    assert result.provider == "openai"
    assert result.configured is True
    assert result.models == ("gpt-4o",)
    assert result.model_metadata["gpt-4o"].context_window == 64_000
    assert result.model_metadata["gpt-4o"].supports_tools is True
    assert result.source == "remote"
    assert result.last_refresh_status == "ok"


def test_provider_catalog_query_builds_empty_result_without_catalog() -> None:
    registry = ModelProviderRegistry(providers={}, model_catalog={})
    query = RuntimeProviderCatalogQuery(registry=registry)

    result = query.models_result("custom", configured=False)

    assert result.provider == "custom"
    assert result.configured is False
    assert result.models == ()
    assert result.model_metadata == {}


@pytest.mark.parametrize("provider_name", ["", "openai/model"])
def test_provider_catalog_query_rejects_invalid_provider_names(provider_name: str) -> None:
    query = _query()

    with pytest.raises(
        ValueError,
        match="provider_name must be a non-empty provider id without '/'",
    ):
        query.catalog_payload(provider_name)
