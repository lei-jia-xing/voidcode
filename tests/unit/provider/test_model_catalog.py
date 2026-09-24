from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import TracebackType
from urllib.request import Request

import pytest

from voidcode.provider import model_catalog
from voidcode.provider.config import AnthropicProviderConfig, GoogleProviderAuthConfig, GoogleProviderConfig, ProviderEndpointConfig
from voidcode.provider.model_catalog import (
    DiscoveryRequest,
    ModelDiscoveryFetchResult,
    ProviderModelMetadata,
    discover_available_models,
)
from voidcode.provider.provider_config import anthropic_compatible_endpoint_config, google_provider_config
from voidcode.provider.provider_table import PROVIDER_TABLE


@dataclass
class _CapturedRequest:
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 0.0


def _capture_discovery_request(monkeypatch: pytest.MonkeyPatch, payload: object | None = None) -> _CapturedRequest:
    """Serve one listing payload and record the request the plan would send."""
    captured = _CapturedRequest()
    response_payload = {"data": [{"id": "provider/model-a"}]} if payload is None else payload

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
            return json.dumps(response_payload).encode("utf-8")

    def _fake_urlopen(request: Request, timeout: float) -> _Response:
        captured.url = request.full_url
        # urllib capitalises header names; HTTP header names are case-insensitive,
        # so compare them lowercased.
        captured.headers = {str(key).lower(): str(value) for key, value in dict(request.header_items()).items()}
        captured.timeout = timeout
        return _Response()

    monkeypatch.setattr(model_catalog, "urlopen", _fake_urlopen)
    return captured


def test_discover_available_models_combines_alias_discovery_and_targets() -> None:
    config = ProviderEndpointConfig(
        base_url="http://127.0.0.1:4000/v1",
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
    assert models.discovery_mode == "configured_base_url"


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
        ProviderEndpointConfig(base_url="https://api.openai.com"),
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
        ProviderEndpointConfig(base_url="https://api.openai.com"),
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


def test_discover_available_models_reports_unavailable_without_a_base_url() -> None:
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
            base_url="https://api.openai.com",
        ),
        fetcher=_failing_fetcher,
    )

    assert result.models == ("alias", "provider/model")
    assert result.source == "fallback"
    assert result.last_refresh_status == "failed"
    assert result.last_error == "remote model discovery failed"
    assert result.discovery_mode == "configured_base_url"


def test_discover_available_models_custom_provider_builds_url_from_plain_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_discovery_request(monkeypatch)

    result = discover_available_models(
        "llama-local",
        ProviderEndpointConfig(base_url="https://gateway.example.com", api_key="k1"),
    )

    assert result.models == ("provider/model-a",)
    assert captured.url == "https://gateway.example.com/v1/models"
    assert captured.timeout == 10.0
    assert result.discovery_mode == "configured_base_url"


def test_discovery_probes_the_configured_openai_compatible_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A gateway is probed at the gateway, not at the vendor's own host.
    captured = _capture_discovery_request(monkeypatch)

    result = discover_available_models(
        "fireworks",
        ProviderEndpointConfig(base_url="https://gateway.example.test/inference/v1", api_key="fw-key"),
    )

    assert captured.url == "https://gateway.example.test/inference/v1/models"
    assert captured.headers["authorization"] == "Bearer fw-key"
    assert result.discovery_mode == "configured_base_url"


def test_discovery_probes_the_configured_anthropic_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_discovery_request(monkeypatch)
    config = anthropic_compatible_endpoint_config(
        "anthropic",
        AnthropicProviderConfig(api_key="anth-key", base_url="https://anthropic-gw.example.test"),
    )

    result = discover_available_models("anthropic", config)

    assert captured.url == "https://anthropic-gw.example.test/v1/models"
    assert captured.headers["anthropic-version"] == "2023-06-01"
    assert captured.headers["x-api-key"] == "anth-key"
    assert result.discovery_mode == "configured_base_url"


def test_anthropic_wire_vendor_sends_its_own_credential_header(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any Anthropic-wire vendor, not just ``anthropic``, gets the Anthropic
    # header set -- and its listing's own credential header.
    captured = _capture_discovery_request(monkeypatch)
    config = anthropic_compatible_endpoint_config("minimax-cn", AnthropicProviderConfig(api_key="mm-key"))

    result = discover_available_models("minimax-cn", config)

    assert captured.url == "https://api.minimaxi.com/anthropic/v1/models"
    assert captured.headers["anthropic-version"] == "2023-06-01"
    assert captured.headers["x-api-key"] == "mm-key"
    assert "authorization" not in captured.headers
    assert result.discovery_mode == "configured_base_url"


def test_anthropic_wire_vendor_with_a_bearer_listing_sends_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_discovery_request(monkeypatch)
    config = anthropic_compatible_endpoint_config("kimi-code", AnthropicProviderConfig(api_key="kimi-key"))

    result = discover_available_models("kimi-code", config)

    assert captured.url == "https://api.kimi.com/coding/v1/models"
    assert captured.headers["anthropic-version"] == "2023-06-01"
    assert captured.headers["authorization"] == "Bearer kimi-key"
    assert result.discovery_mode == "configured_base_url"


def test_discovery_is_disabled_for_a_vendor_without_a_listing() -> None:
    # Copilot publishes no listing: the plan reports disabled instead of probing.
    result = discover_available_models(
        "github-copilot",
        ProviderEndpointConfig(base_url="https://api.individual.githubcopilot.com", api_key="copilot-token"),
    )

    assert result.discovery_mode == "disabled"
    assert result.last_refresh_status == "skipped"
    assert result.last_error == "provider has no model listing"


def test_discovery_probes_the_google_wire_path_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_discovery_request(monkeypatch, payload={"models": [{"name": "models/gemini-3-flash"}]})
    config = google_provider_config(
        GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(method="api_key", api_key="google-key"),
            base_url="https://google-gw.example.test",
        )
    )

    result = discover_available_models("google", config)

    assert captured.url == "https://google-gw.example.test/v1beta/models"
    assert captured.headers["x-goog-api-key"] == "google-key"
    assert result.models == ("gemini-3-flash",)
    assert result.discovery_mode == "configured_base_url"


def test_anthropic_wire_vendor_uses_the_shared_data_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``data`` may hold bare model-id strings; only the shared parser accepts
    # that shape, so parsing one proves the Anthropic wire routes through it.
    captured = _capture_discovery_request(monkeypatch, payload={"data": ["kimi-k2", {"id": "kimi-k3"}]})
    config = anthropic_compatible_endpoint_config("kimi-code", AnthropicProviderConfig(api_key="kimi-key"))

    result = discover_available_models("kimi-code", config)

    assert captured.url == "https://api.kimi.com/coding/v1/models"
    assert result.models == ("kimi-k2", "kimi-k3")


def test_discovery_dispatch_keys_on_the_wire_not_the_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    anthropic_wire = ProviderEndpointConfig(
        base_url="https://api.minimaxi.com/anthropic",
        api_key="mm-key",
        auth_header="X-Api-Key",
        auth_scheme="token",
        wire="anthropic-messages",
    )

    captured = _capture_discovery_request(monkeypatch)
    discover_available_models("fireworks", anthropic_wire)

    assert captured.headers["anthropic-version"] == "2023-06-01"
    assert captured.headers["x-api-key"] == "mm-key"
    assert "authorization" not in captured.headers

    openai_wire = ProviderEndpointConfig(base_url="https://gw.example.test/v1", api_key="gw-key")

    captured = _capture_discovery_request(monkeypatch)
    discover_available_models("anthropic", openai_wire)

    assert captured.url == "https://gw.example.test/v1/models"
    assert "anthropic-version" not in captured.headers
    assert captured.headers["authorization"] == "Bearer gw-key"


def test_shipped_catalog_covers_exactly_the_providers_with_upstream_keys() -> None:
    # The generator emits one catalog entry per provider-table row that names
    # models.dev keys, so a table edit that is not followed by a regeneration
    # (or a hand-edited artifact) fails here.
    expected = {row.id for row in PROVIDER_TABLE if row.models_dev_keys}

    assert set(model_catalog._load_static_catalog()) == expected


@pytest.mark.parametrize(
    ("wire", "base_url", "expected"),
    [
        # A base that already names its version mid-path: the listing hangs off the
        # base itself (deepinfra's `.../v1/openai` -> live listing at `.../openai/models`;
        # the old rule appended another `/v1/models` and 404'd). Every other shape is
        # the URL the P5 probe observed live.
        ("openai-completions", "https://api.deepinfra.com/v1/openai", "https://api.deepinfra.com/v1/openai/models"),
        ("openai-completions", "https://api.novita.ai/openai/v1", "https://api.novita.ai/openai/v1/models"),
        ("openai-completions", "https://api.deepseek.com", "https://api.deepseek.com/v1/models"),
        (
            "openai-completions",
            "https://dashscope.aliyuncs.com/compatible-mode",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        ),
        ("openai-completions", "https://api.x.ai", "https://api.x.ai/v1/models"),
        ("openai-completions", "https://gw.example.test/v1/models", "https://gw.example.test/v1/models"),
        ("anthropic-messages", "https://api.anthropic.com", "https://api.anthropic.com/v1/models"),
        ("anthropic-messages", "https://api.kimi.com/coding", "https://api.kimi.com/coding/v1/models"),
        ("google-generative-ai", "https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta/models"),
        ("google-generative-ai", "https://gw.example.test/v1beta", "https://gw.example.test/v1beta/models"),
    ],
)
def test_the_listing_url_rule_matches_the_probed_paths(wire: str, base_url: str, expected: str) -> None:
    assert model_catalog._models_url(wire, base_url) == expected


def test_the_version_mid_path_base_is_probed_at_its_own_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule above is the one the fetch actually uses, not a parallel copy."""
    config = ProviderEndpointConfig(base_url="https://api.deepinfra.com/v1/openai", api_key="k")

    captured = _capture_discovery_request(monkeypatch)
    discover_available_models("deepinfra", config)

    assert captured.url == "https://api.deepinfra.com/v1/openai/models"
