from __future__ import annotations

import json
from types import TracebackType
from urllib.request import Request

import pytest

from voidcode.provider import model_catalog
from voidcode.provider.config import ProviderEndpointConfig
from voidcode.provider.model_catalog import (
    DiscoveryRequest,
    ModelDiscoveryFetchResult,
    ProviderModelMetadata,
    discover_available_models,
)


def test_discover_available_models_combines_alias_discovery_and_targets() -> None:
    config = ProviderEndpointConfig(
        discovery_base_url="http://127.0.0.1:4000",
        model_map={
            "alias-a": "provider/model-a",
            "alias-b": "provider/model-b",
        },
    )

    models = discover_available_models(
        "endpoint",
        config,
        fetcher=lambda _request: ("provider/model-a", "provider/model-c"),
    )

    assert models.models == (
        "alias-a",
        "alias-b",
        "provider/model-a",
        "provider/model-c",
        "provider/model-b",
    )
    assert models.source in {"remote", "mixed"}
    assert models.discovery_mode == "configured_endpoint"


def test_discover_available_models_fills_gaps_from_static_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_catalog,
        "static_catalog_metadata",
        lambda *_: ProviderModelMetadata(context_window=111),
    )
    result = discover_available_models(
        "openai",
        ProviderEndpointConfig(discovery_base_url="https://api.openai.com"),
        fetcher=lambda _request: ModelDiscoveryFetchResult(
            models=("gpt-5",),
            model_metadata={},
        ),
    )

    assert result.model_metadata["gpt-5"].context_window == 111


def test_static_catalog_metadata_lowercases_and_looks_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_catalog,
        "_load_static_catalog",
        lambda: {"openai": {"gpt-5": ProviderModelMetadata(context_window=400_000)}},
    )

    assert model_catalog.static_catalog_metadata("OpenAI", "GPT-5").context_window == 400_000
    assert model_catalog.static_catalog_metadata("openai", "nope") is None


def test_discover_available_models_recomputes_input_limit_for_remote_context_override() -> None:
    result = discover_available_models(
        "openai",
        ProviderEndpointConfig(discovery_base_url="https://api.openai.com"),
        fetcher=lambda _request: ModelDiscoveryFetchResult(
            models=("gpt-4o",),
            model_metadata={
                "gpt-4o": ProviderModelMetadata(
                    context_window=64_000,
                    max_output_tokens=16_384,
                )
            },
        ),
    )

    metadata = result.model_metadata["gpt-4o"]
    assert metadata.context_window == 64_000
    assert metadata.max_output_tokens == 16_384
    assert metadata.max_input_tokens == 47_616


def test_provider_model_metadata_payload_includes_limits_and_capabilities() -> None:
    payload = ProviderModelMetadata(
        context_window=128_000,
        max_output_tokens=16_384,
        supports_tools=True,
        supports_vision=False,
        supports_streaming=True,
        cost_per_input_token=0.000001,
        cost_per_output_token=0.000002,
        supports_reasoning_effort=True,
        default_reasoning_effort="low",
        supports_reasoning_summary=True,
        supports_thinking_budget=False,
        supports_interleaved_reasoning=False,
        reasoning_visibility="summary",
        modalities_input=("text",),
        modalities_output=("text",),
        model_status="active",
        tool_feedback_mode="synthetic_user_message",
    ).payload()

    assert payload == {
        "context_window": 128_000,
        "max_input_tokens": 111_616,
        "max_output_tokens": 16_384,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "cost_per_input_token": 0.000001,
        "cost_per_output_token": 0.000002,
        "supports_reasoning_effort": True,
        "default_reasoning_effort": "low",
        "supports_reasoning_summary": True,
        "supports_thinking_budget": False,
        "supports_interleaved_reasoning": False,
        "reasoning_visibility": "summary",
        "modalities_input": ["text"],
        "modalities_output": ["text"],
        "model_status": "active",
        "tool_feedback_mode": "synthetic_user_message",
    }


def test_discover_available_models_skips_when_no_discovery_base_url_or_base_url() -> None:
    result = discover_available_models("openai", ProviderEndpointConfig(api_key="sk-test"))

    assert result.source == "fallback"
    assert result.last_refresh_status == "skipped"
    assert result.last_error == "provider has no model discovery endpoint"
    assert result.discovery_mode == "unavailable"


def test_discover_available_models_marks_fallback_on_fetch_failure() -> None:
    def _failing_fetcher(_request: DiscoveryRequest) -> tuple[str, ...]:
        raise TimeoutError("timed out")

    result = discover_available_models(
        "openai",
        ProviderEndpointConfig(
            model_map={"alias": "provider/model"},
            discovery_base_url="https://api.openai.com",
        ),
        fetcher=_failing_fetcher,
    )

    assert result.models == ("alias", "provider/model")
    assert result.source == "fallback"
    assert result.last_refresh_status == "failed"
    assert result.last_error == "remote model discovery failed"
    assert result.discovery_mode == "configured_endpoint"


def test_discover_available_models_custom_provider_builds_url_from_plain_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"data": [{"id": "provider/model-a"}]}).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = {str(key): str(value) for key, value in dict(request.header_items()).items()}
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "llama-local",
        ProviderEndpointConfig(base_url="https://gateway.example.com", api_key="k1"),
    )

    assert result.models == ("provider/model-a",)
    assert captured["url"] == "https://gateway.example.com/v1/models"
    assert captured["timeout"] == 10.0
    assert result.discovery_mode == "configured_base_url"
