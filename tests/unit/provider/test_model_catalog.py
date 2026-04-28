from __future__ import annotations

import json
from types import TracebackType
from typing import cast
from urllib.request import Request

import pytest

from voidcode.provider import model_catalog
from voidcode.provider.config import LiteLLMProviderConfig
from voidcode.provider.model_catalog import (
    DiscoveryRequest,
    ModelDiscoveryFetchResult,
    ProviderModelMetadata,
    discover_available_models,
    infer_model_metadata,
)


def test_discover_available_models_combines_alias_discovery_and_targets() -> None:
    config = LiteLLMProviderConfig(
        discovery_base_url="http://127.0.0.1:4000",
        model_map={
            "alias-a": "provider/model-a",
            "alias-b": "provider/model-b",
        },
    )

    models = discover_available_models(
        "litellm",
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


def test_discover_available_models_for_openai_uses_endpoint_fetcher() -> None:
    captured: list[DiscoveryRequest] = []

    def _fetcher(request: DiscoveryRequest) -> tuple[str, ...]:
        captured.append(request)
        return ("gpt-4o",)

    config = LiteLLMProviderConfig(
        api_key="sk-test",
        discovery_base_url="https://api.openai.com",
    )
    models = discover_available_models("openai", config, fetcher=_fetcher)

    assert models.models == ("gpt-4o",)
    assert len(captured) == 1
    assert captured[0].provider == "openai"
    assert captured[0].base_url == "https://api.openai.com"
    assert captured[0].headers == {"Authorization": "Bearer sk-test"}
    assert captured[0].timeout_seconds == 10.0
    assert models.discovery_mode == "configured_endpoint"


def test_discover_available_models_includes_known_model_budget_metadata() -> None:
    result = discover_available_models(
        "openai",
        LiteLLMProviderConfig(discovery_base_url="https://api.openai.com"),
        fetcher=lambda _request: ("gpt-4o", "unknown-model"),
    )

    metadata = result.model_metadata["gpt-4o"]
    assert metadata.context_window == 128_000
    assert metadata.max_input_tokens == 111_616
    assert metadata.max_output_tokens == 16_384
    assert metadata.supports_tools is True
    assert metadata.supports_vision is True
    assert metadata.cost_per_input_token is not None
    assert metadata.modalities_input == ("text", "image")
    assert "unknown-model" not in result.model_metadata


def test_discover_available_models_prefers_remote_metadata_when_present() -> None:
    result = discover_available_models(
        "openai",
        LiteLLMProviderConfig(discovery_base_url="https://api.openai.com"),
        fetcher=lambda _request: ModelDiscoveryFetchResult(
            models=("gpt-4o",),
            model_metadata={
                "gpt-4o": ProviderModelMetadata(
                    context_window=64_000,
                    max_output_tokens=4_096,
                    cost_per_input_token=0.000001,
                    supports_tools=True,
                    modalities_input=("text",),
                    model_status="preview",
                )
            },
        ),
    )

    metadata = result.model_metadata["gpt-4o"]
    assert metadata.context_window == 64_000
    assert metadata.max_input_tokens == 59_904
    assert metadata.cost_per_input_token == 0.000001
    assert metadata.modalities_input == ("text",)
    assert metadata.model_status == "preview"


@pytest.mark.parametrize(
    ("provider", "model", "context_window", "max_output_tokens"),
    [
        ("openai", "gpt-5.5", 1_000_000, 128_000),
        ("anthropic", "claude-opus-4-7", 1_000_000, 64_000),
        ("google", "gemini-3-pro-preview", 1_048_576, 65_536),
        ("deepseek", "deepseek-v4-pro", 1_000_000, 384_000),
        ("qwen", "qwen3.6-plus", 1_000_000, 64_000),
        ("glm", "glm-5.1", 198_000, 128_000),
        ("kimi", "kimi-k2.6", 256_000, 96_000),
        ("minimax", "MiniMax-M2.5", 192_000, 32_000),
        ("grok", "grok-4-1-fast-reasoning", 2_000_000, 30_000),
    ],
)
def test_infer_model_metadata_covers_current_frontier_provider_models(
    provider: str,
    model: str,
    context_window: int,
    max_output_tokens: int,
) -> None:
    metadata = infer_model_metadata(provider, model)
    assert metadata is not None
    assert metadata.context_window == context_window
    assert metadata.max_output_tokens == max_output_tokens
    assert metadata.cost_per_input_token is not None
    assert metadata.cost_per_output_token is not None
    assert metadata.modalities_input is not None
    assert metadata.modalities_output == ("text",)
    assert metadata.model_status in {"active", "preview"}


def test_infer_model_metadata_exposes_model_capability_flags() -> None:
    metadata = infer_model_metadata("anthropic", "claude-sonnet-4-6")

    assert metadata == ProviderModelMetadata(
        context_window=1_000_000,
        max_output_tokens=64_000,
        supports_tools=True,
        supports_vision=True,
        supports_streaming=True,
        supports_reasoning=True,
        supports_json_mode=False,
        cost_per_input_token=0.000003,
        cost_per_output_token=0.000015,
        supports_reasoning_effort=True,
        default_reasoning_effort="medium",
        supports_interleaved_reasoning=True,
        modalities_input=("text", "image"),
        modalities_output=("text",),
        model_status="active",
    )


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
        supports_interleaved_reasoning=False,
        modalities_input=("text",),
        modalities_output=("text",),
        model_status="active",
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
        "supports_interleaved_reasoning": False,
        "modalities_input": ["text"],
        "modalities_output": ["text"],
        "model_status": "active",
    }


def test_infer_model_metadata_returns_none_for_unknown_models() -> None:
    assert infer_model_metadata("custom", "local-demo") is None


def test_discover_available_models_for_anthropic_uses_provider_specific_headers() -> None:
    captured: list[DiscoveryRequest] = []

    def _fetcher(request: DiscoveryRequest) -> tuple[str, ...]:
        captured.append(request)
        return ("claude-3-7-sonnet-latest",)

    config = LiteLLMProviderConfig(
        api_key="sk-ant-test",
        discovery_base_url="https://api.anthropic.com",
    )
    models = discover_available_models("anthropic", config, fetcher=_fetcher)

    assert models.models == ("claude-3-7-sonnet-latest",)
    assert len(captured) == 1
    assert captured[0].provider == "anthropic"
    assert captured[0].base_url == "https://api.anthropic.com"
    assert captured[0].headers == {
        "anthropic-version": "2023-06-01",
        "x-api-key": "sk-ant-test",
    }
    assert models.discovery_mode == "configured_endpoint"


def test_discover_available_models_for_google_uses_google_base_url() -> None:
    captured: list[DiscoveryRequest] = []

    def _fetcher(request: DiscoveryRequest) -> tuple[str, ...]:
        captured.append(request)
        return ("gemini-2.0-flash",)

    config = LiteLLMProviderConfig(
        api_key="AIza-test",
        discovery_base_url="https://generativelanguage.googleapis.com",
    )
    models = discover_available_models("google", config, fetcher=_fetcher)

    assert models.models == ("gemini-2.0-flash",)
    assert len(captured) == 1
    assert captured[0].provider == "google"
    assert captured[0].base_url == "https://generativelanguage.googleapis.com"
    assert captured[0].api_key == "AIza-test"
    assert captured[0].headers == {"x-goog-api-key": "AIza-test"}
    assert models.discovery_mode == "configured_endpoint"


def test_discover_available_models_for_google_with_api_key_header_does_not_append_key_query_param(
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
            return json.dumps({"models": [{"name": "models/gemini-2.0-flash"}]}).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = {
            str(key).lower(): str(value) for key, value in dict(request.header_items()).items()
        }
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "google",
        LiteLLMProviderConfig(
            base_url="https://generativelanguage.googleapis.com",
            api_key="AIza-test",
            auth_header="x-goog-api-key",
            auth_scheme="token",
        ),
    )

    assert result.models == ("gemini-2.0-flash",)
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models"
    headers = cast(dict[str, str], captured["headers"])
    assert headers["x-goog-api-key"] == "AIza-test"


def test_discover_available_models_for_google_with_oauth_header_does_not_append_key_query_param(
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
            return json.dumps({"models": [{"name": "models/gemini-2.0-flash"}]}).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "google",
        LiteLLMProviderConfig(
            base_url="https://generativelanguage.googleapis.com",
            api_key="oauth-token",
            auth_header="Authorization",
            auth_scheme="bearer",
        ),
    )

    assert result.models == ("gemini-2.0-flash",)
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models"


def test_discover_available_models_skips_when_no_discovery_base_url_or_base_url() -> None:
    result = discover_available_models("openai", LiteLLMProviderConfig(api_key="sk-test"))

    assert result.source == "fallback"
    assert result.last_refresh_status == "skipped"
    assert result.last_error == "provider has no model discovery endpoint"
    assert result.discovery_mode == "unavailable"


def test_discover_available_models_marks_fallback_on_fetch_failure() -> None:
    def _failing_fetcher(_request: DiscoveryRequest) -> tuple[str, ...]:
        raise TimeoutError("timed out")

    result = discover_available_models(
        "openai",
        LiteLLMProviderConfig(
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


def test_discover_available_models_falls_back_on_invalid_openai_discovery_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            return json.dumps(["not-an-object"]).encode("utf-8")

    def _fake_urlopen(_request: Request, timeout: float) -> _Response:
        assert timeout == 10.0
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "openai",
        LiteLLMProviderConfig(
            discovery_base_url="https://api.openai.com",
            model_map={"alias": "gpt-4o"},
        ),
    )

    assert result.models == ("alias", "gpt-4o")
    assert result.source == "fallback"
    assert result.last_refresh_status == "failed"
    assert result.last_error == "remote model discovery failed"


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
        captured["headers"] = {
            str(key): str(value) for key, value in dict(request.header_items()).items()
        }
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "llama-local",
        LiteLLMProviderConfig(base_url="https://gateway.example.com", api_key="k1"),
    )

    assert result.models == ("provider/model-a",)
    assert captured["url"] == "https://gateway.example.com/v1/models"
    assert captured["timeout"] == 10.0
    assert result.discovery_mode == "configured_base_url"


def test_discover_available_models_custom_provider_keeps_existing_v1_models_path(
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
            return json.dumps({"data": [{"id": "provider/model-b"}]}).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = {
            str(key): str(value) for key, value in dict(request.header_items()).items()
        }
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "llama-local",
        LiteLLMProviderConfig(base_url="https://gateway.example.com/v1/models", api_key="k2"),
    )

    assert result.models == ("provider/model-b",)
    assert captured["url"] == "https://gateway.example.com/v1/models"
    assert result.discovery_mode == "configured_base_url"


def test_discover_available_models_glm_base_url_uses_v4_models_path(
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
            return json.dumps({"data": [{"id": "glm/glm-4-flash"}]}).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "glm",
        LiteLLMProviderConfig(base_url="https://open.bigmodel.cn/api/paas/v4", api_key="glm-key"),
    )

    assert result.models == ("glm/glm-4-flash",)
    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/models"
    assert result.discovery_mode == "configured_base_url"


def test_discover_available_models_respects_discovery_base_url_when_set(
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
            return json.dumps({"data": [{"id": "openai/gpt-4o"}]}).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "opencode-go",
        LiteLLMProviderConfig(
            base_url="https://opencode.ai/zen/go",
            discovery_base_url="https://opencode.ai/zen/v1",
            api_key="opencode-go-key",
        ),
    )

    assert result.models == ("openai/gpt-4o",)
    assert captured["url"] == "https://opencode.ai/zen/v1/models"
    assert result.discovery_mode == "configured_endpoint"


def test_discover_available_models_skips_remote_when_discovery_base_url_is_empty() -> None:
    config = LiteLLMProviderConfig(
        base_url="https://api.minimax.io",
        discovery_base_url="",
        model_map={"alias": "MiniMax-M2.7"},
    )

    result = discover_available_models("minimax", config)

    assert result.models == ("alias", "MiniMax-M2.7")
    assert result.source == "fallback"
    assert result.last_refresh_status == "skipped"
    assert result.last_error == "provider model discovery disabled by config"
    assert result.discovery_mode == "disabled"


def test_discover_available_models_custom_provider_uses_token_auth_header_without_bearer_prefix(
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
            return json.dumps({"data": [{"id": "provider/model-c"}]}).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = {
            str(key): str(value) for key, value in dict(request.header_items()).items()
        }
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)

    result = discover_available_models(
        "llama-local",
        LiteLLMProviderConfig(
            base_url="https://gateway.example.com/v1",
            api_key="token-raw",
            auth_scheme="token",
            auth_header="X-API-Key",
        ),
    )

    assert result.models == ("provider/model-c",)
    assert captured["url"] == "https://gateway.example.com/v1/models"
    headers = {
        str(k).lower(): str(v) for k, v in dict(cast(dict[str, str], captured["headers"])).items()
    }
    assert headers.get("x-api-key") == "token-raw"
    assert result.discovery_mode == "configured_base_url"
