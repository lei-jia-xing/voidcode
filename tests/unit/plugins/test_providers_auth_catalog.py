"""Provider authentication, credential delivery, and catalog pricing.

Every credential assertion is a recorded wire fact from a local loopback server
(plain HTTP or self-signed TLS), or the typed refusal a provider raises before a
request exists. No test reaches a real host: the Google service-account flow
signs its assertion locally and exchanges it at a ``token_uri`` on loopback.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import cast
from urllib.parse import unquote_plus

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.capabilities.providers.dispatch import RegisteredProvider, build_registered_providers
from voidcode.capabilities.providers.native.model_catalog import static_catalog_metadata
from voidcode.capabilities.providers.pricing import price_usage, usage_cost_usd
from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderMessage,
    ProviderTokenUsage,
    ProviderTurnRequest,
)

_ANTHROPIC_REPLY: dict[str, object] = {
    "id": "msg_loopback",
    "type": "message",
    "model": "claude-loopback",
    "content": [{"type": "text", "text": "anthropic-ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 4, "cache_read_input_tokens": 2},
}
_GEMINI_REPLY: dict[str, object] = {
    "candidates": [{"content": {"parts": [{"text": "google-ok"}], "role": "model"}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
}
_TOKEN_REPLY: dict[str, object] = {"access_token": "loopback-access-token", "expires_in": 3600, "token_type": "Bearer"}
_OPENAI_REPLY: dict[str, object] = {
    "id": "chatcmpl-json-loopback",
    "object": "chat.completion",
    "created": 1,
    "model": "loopback-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "endpoint-ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


@dataclass(frozen=True, slots=True)
class _RecordedRequest:
    path: str
    headers: Mapping[str, str]
    body: bytes

    @property
    def form(self) -> Mapping[str, str]:
        decoded = self.body.decode("utf-8")
        return {key: unquote_plus(value) for key, _, value in (part.partition("=") for part in decoded.split("&"))}


def _reply_for(path: str) -> dict[str, object]:
    if path.startswith("/token"):
        return dict(_TOKEN_REPLY)
    if "/messages" in path:
        return dict(_ANTHROPIC_REPLY)
    if "generateContent" in path:
        return dict(_GEMINI_REPLY)
    if path.endswith("/chat/completions"):
        return dict(_OPENAI_REPLY)
    raise AssertionError(f"unexpected loopback path: {path}")


class _JsonLoopback(ThreadingHTTPServer):
    """Records every request and answers it with the reply its path selects."""

    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), _JsonLoopbackHandler)
        self.delay_seconds = delay_seconds
        self.requests: list[_RecordedRequest] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _JsonLoopbackHandler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        server = cast(_JsonLoopback, self.server)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server.requests.append(_RecordedRequest(self.path, {name.lower(): value for name, value in self.headers.items()}, body))
        if server.delay_seconds:
            time.sleep(server.delay_seconds)
        encoded = json.dumps(_reply_for(self.path)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            # The client already hung up: this test's subject is that hang-up.
            return

    do_POST = _handle
    do_GET = _handle

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


@dataclass
class _Servers:
    """Every loopback a test started, torn down together."""

    http: list[_JsonLoopback]
    openai: list[OpenAILoopbackServer]
    threads: list[threading.Thread]

    def start_json(self, *, delay_seconds: float = 0.0) -> _JsonLoopback:
        server = _JsonLoopback(delay_seconds=delay_seconds)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.http.append(server)
        self.threads.append(thread)
        return server

    def start_openai(self, *, tls: tuple[Path, Path] | None = None) -> OpenAILoopbackServer:
        """Start a chat-completions loopback; TLS is installed before the accept loop owns the socket."""
        server = OpenAILoopbackServer([])
        if tls is not None:
            certificate, private_key = tls
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, private_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.openai.append(server)
        self.threads.append(thread)
        return server

    def stop(self) -> None:
        for server in (*self.http, *self.openai):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)


@pytest.fixture
def servers() -> Iterator[_Servers]:
    started = _Servers([], [], [])
    try:
        yield started
    finally:
        started.stop()


def _base_url(server: OpenAILoopbackServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/v1"


def _provider(entries: dict[str, object], *, name: str = "deepseek") -> RegisteredProvider:
    providers = {provider.name: provider for provider in build_registered_providers({"providers": entries})}
    assert name in providers
    return providers[name]


def _request(*messages: ProviderMessage, **overrides: object) -> ProviderTurnRequest:
    fields: dict[str, object] = {
        "messages": messages or (ProviderMessage("user", "hi"),),
        "raw_model": "deepseek/deepseek-chat",
        "provider_name": "deepseek",
        "model_name": "deepseek-chat",
        "reasoning_effort": None,
    }
    fields.update(overrides)
    return ProviderTurnRequest(**fields)  # type: ignore[arg-type]


def _self_signed_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "loopback-cert.pem"
    private_key_path = directory / "loopback-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return certificate_path, private_key_path


def _service_account_file(directory: Path, *, token_uri: str, name: str = "service-account.json") -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode("utf-8")
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "loopback-project",
                "private_key_id": "loopback-key-id",
                "private_key": private_key,
                "client_email": "loopback@loopback-project.iam.gserviceaccount.com",
                "client_id": "1",
                "token_uri": token_uri,
                "universe_domain": "googleapis.com",
            }
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Credential shapes on the wire
# ---------------------------------------------------------------------------


def test_an_api_key_reaches_the_openai_wire_as_a_bearer_header(servers: _Servers) -> None:
    server = servers.start_openai()
    server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    provider = _provider({"deepseek": {"api_key": "deepseek-secret", "base_url": _base_url(server)}})
    provider.propose_turn(_request())

    assert server.request_headers[0]["authorization"] == "Bearer deepseek-secret"
    assert server.request_headers[0]["content-type"] == "application/json"


def test_endpoint_auth_scheme_decides_which_header_carries_the_credential(servers: _Servers) -> None:
    server = servers.start_openai()
    for _ in range(3):
        server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    token_scheme = _provider(
        {"token-gateway": {"api_key": "raw-token", "base_url": _base_url(server), "auth_header": "x-api-key", "auth_scheme": "token"}},
        name="endpoint",
    )
    no_scheme = _provider(
        {"open-gateway": {"api_key": "unused-token", "base_url": _base_url(server), "auth_scheme": "none"}},
        name="endpoint",
    )
    custom_header = _provider(
        {"custom-gateway": {"api_key": "custom-token", "base_url": _base_url(server), "auth_header": "x-custom-auth", "auth_scheme": "bearer"}},
        name="endpoint",
    )

    token_scheme.propose_turn(_request(provider_name="token-gateway", model_name="loopback-model"))
    no_scheme.propose_turn(_request(provider_name="open-gateway", model_name="loopback-model"))
    custom_header.propose_turn(_request(provider_name="custom-gateway", model_name="loopback-model"))

    token_headers, none_headers, custom_headers = server.request_headers
    assert token_headers["x-api-key"] == "raw-token"
    assert "authorization" not in token_headers
    # ``none`` sends no credential at all, even though an API key is configured.
    assert "authorization" not in none_headers and "x-api-key" not in none_headers
    assert not any(name.startswith("x-custom") for name in none_headers)
    assert custom_headers["x-custom-auth"] == "Bearer custom-token"
    assert "authorization" not in custom_headers


def test_openai_organization_and_project_reach_the_wire(servers: _Servers) -> None:
    server = servers.start_openai()
    server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    provider = _provider(
        {"openai": {"api_key": "openai-secret", "base_url": _base_url(server), "organization": "org-loopback", "project": "proj-loopback"}},
        name="openai",
    )
    provider.propose_turn(_request(provider_name="openai", model_name="gpt-4o-mini", raw_model="openai/gpt-4o-mini"))

    headers = server.request_headers[0]
    assert headers["openai-organization"] == "org-loopback"
    assert headers["openai-project"] == "proj-loopback"
    assert headers["authorization"] == "Bearer openai-secret"


def test_anthropic_version_and_beta_headers_carry_the_configured_values(servers: _Servers) -> None:
    server = servers.start_json()
    provider = _provider(
        {
            "anthropic": {
                "api_key": "anthropic-secret",
                "base_url": server.base_url,
                "version": "2024-11-05",
                "beta_headers": ["prompt-caching-2024-07-31", "context-1m-2025-08-07"],
            }
        },
        name="anthropic",
    )

    result = provider.propose_turn(_request(provider_name="anthropic", model_name="claude-sonnet-4-5", raw_model="anthropic/claude-sonnet-4-5"))

    assert result.output == "anthropic-ok"
    assert result.usage is not None
    # Anthropic's own ``input_tokens`` is the uncached half; the plugin normalises
    # it to the inclusive prompt total every other wire reports.
    assert (result.usage.input_tokens, result.usage.cache_read_tokens, result.usage.uncached_input_tokens) == (13, 2, 11)

    request = server.requests[0]
    assert request.path == "/v1/messages"
    assert request.headers["x-api-key"] == "anthropic-secret"
    assert request.headers["anthropic-version"] == "2024-11-05"
    assert request.headers["anthropic-beta"] == "prompt-caching-2024-07-31,context-1m-2025-08-07"
    assert "authorization" not in request.headers


def test_google_api_key_and_oauth_use_their_respective_auth_headers(servers: _Servers) -> None:
    server = servers.start_json()
    api_key_provider = _provider(
        {"google": {"api_key": "google-api-key", "base_url": server.base_url}},
        name="google",
    )
    oauth_provider = _provider(
        {
            "google": {
                "auth": {"method": "oauth", "access_token": "google-access-token"},
                "base_url": server.base_url,
                "project": "loopback-project",
                "region": "us-central1",
            }
        },
        name="google",
    )

    api_key_provider.propose_turn(_request(provider_name="google", model_name="gemini-2.5-flash", raw_model="google/gemini-2.5-flash"))
    oauth_provider.propose_turn(_request(provider_name="google", model_name="gemini-2.5-flash", raw_model="google/gemini-2.5-flash"))

    api_key_request, oauth_request = server.requests
    assert "generateContent" in api_key_request.path
    assert api_key_request.headers["x-goog-api-key"] == "google-api-key"
    assert oauth_request.headers["authorization"] == "Bearer google-access-token"
    assert "x-goog-api-key" not in oauth_request.headers
    assert "/projects/loopback-project/locations/us-central1/" in oauth_request.path


def test_google_service_account_signs_and_exchanges_its_assertion_locally(servers: _Servers, tmp_path: Path) -> None:
    server = servers.start_json()
    service_account = _service_account_file(tmp_path, token_uri=f"{server.base_url}/token")
    provider = _provider(
        {
            "google": {
                "auth": {"method": "service_account", "service_account_json_path": str(service_account)},
                "base_url": server.base_url,
                "project": "loopback-project",
                "region": "us-central1",
            }
        },
        name="google",
    )

    result = provider.propose_turn(_request(provider_name="google", model_name="gemini-2.5-flash", raw_model="google/gemini-2.5-flash"))

    assert result.output == "google-ok"
    token_request, model_request = server.requests
    assert token_request.path == "/token"
    assert token_request.form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert token_request.form["assertion"]  # a signed JWT, not a fabricated token
    assert "/projects/loopback-project/locations/us-central1/" in model_request.path
    assert model_request.headers["authorization"] == "Bearer loopback-access-token"
    assert "x-goog-api-key" not in model_request.headers


def test_google_service_account_failures_are_typed_and_leak_no_key_material(servers: _Servers, tmp_path: Path) -> None:
    server = servers.start_json()
    secret = "sa-secret-must-never-be-logged"
    unreadable = tmp_path / f"{secret}.json"
    unreadable.write_text("not a service account", encoding="utf-8")

    for path in (tmp_path / "absent.json", unreadable):
        provider = _provider(
            {"google": {"auth": {"method": "service_account", "service_account_json_path": str(path)}, "base_url": server.base_url}},
            name="google",
        )
        with pytest.raises(ProviderExecutionError) as excinfo:
            provider.propose_turn(_request(provider_name="google", model_name="gemini-2.5-flash", raw_model="google/gemini-2.5-flash"))

        error = excinfo.value
        assert error.kind == "missing_auth"
        assert error.retryable is False
        assert "service_account_json_path" in error.message
        assert secret not in f"{error.message} {error.details} {error!r}"
        assert str(path) not in f"{error.message} {error.details} {error!r}"

    # The loader never ran, so nothing was sent anywhere.
    assert server.requests == []


def test_copilot_token_reaches_the_wire_as_a_bearer_header(servers: _Servers) -> None:
    server = servers.start_openai()
    server.replies.append(openai_response(message={"content": "copilot-ok"}, finish="stop"))

    provider = _provider(
        {"github-copilot": {"api_key": "copilot-secret", "base_url": _base_url(server)}},
        name="github-copilot",
    )

    result = provider.propose_turn(_request(provider_name="github-copilot", model_name="gpt-4o", raw_model="github-copilot/gpt-4o"))

    assert result.output == "copilot-ok"
    assert server.request_headers[0]["authorization"] == "Bearer copilot-secret"


def test_endpoint_model_map_alias_is_what_reaches_the_wire(servers: _Servers) -> None:
    server = servers.start_openai()
    server.replies.append(openai_response(message={"content": "ok"}, finish="stop"))

    provider = _provider(
        {"alias-gateway": {"api_key": "loopback-key", "base_url": _base_url(server), "model_map": {"alias-model": "upstream-model"}}},
        name="endpoint",
    )
    provider.propose_turn(_request(provider_name="alias-gateway", model_name="alias-model", raw_model="alias-gateway/alias-model"))

    assert server.requests[0]["model"] == "upstream-model"


def test_endpoint_timeout_seconds_bounds_the_request(servers: _Servers) -> None:
    server = servers.start_json(delay_seconds=1.0)
    provider = _provider(
        {"slow-gateway": {"api_key": "loopback-key", "base_url": f"{server.base_url}/v1", "timeout_seconds": 0.1}},
        name="endpoint",
    )

    started = time.monotonic()
    with pytest.raises(ProviderExecutionError) as excinfo:
        provider.propose_turn(_request(provider_name="slow-gateway", model_name="loopback-model", raw_model="slow-gateway/loopback-model"))
    elapsed = time.monotonic() - started

    assert excinfo.value.kind == "provider_error"
    assert excinfo.value.retryable is True
    assert excinfo.value.details is not None
    assert excinfo.value.details["network_failure"] is True
    # The configured 0.1s budget, not the server's 1s delay, ended the request.
    assert elapsed < 0.8
    assert len(server.requests) == 1


def test_endpoint_ssl_verify_passes_through_to_the_http_client(servers: _Servers, tmp_path: Path) -> None:
    certificate = _self_signed_certificate(tmp_path)
    server = servers.start_openai(tls=certificate)
    base_url = f"https://127.0.0.1:{server.server_port}/v1"
    verifying = _provider({"tls-gateway": {"api_key": "loopback-key", "base_url": base_url}}, name="endpoint")
    trusting = _provider({"tls-gateway": {"api_key": "loopback-key", "base_url": base_url, "ssl_verify": False}}, name="endpoint")
    server.replies.append(openai_response(message={"content": "tls-ok"}, finish="stop"))

    with pytest.raises(ProviderExecutionError) as excinfo:
        verifying.propose_turn(_request(provider_name="tls-gateway", model_name="loopback-model", raw_model="tls-gateway/loopback-model"))

    assert excinfo.value.kind == "provider_error"
    assert excinfo.value.retryable is True
    # The handshake failed, so the self-signed host was never told anything.
    assert server.requests == []

    result = trusting.propose_turn(_request(provider_name="tls-gateway", model_name="loopback-model", raw_model="tls-gateway/loopback-model"))

    assert result.output == "tls-ok"
    assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# Auth methods, authorize material, callback state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Credential confinement
# ---------------------------------------------------------------------------


def test_credentials_never_appear_in_a_registered_provider_repr_or_in_an_error(servers: _Servers) -> None:
    server = servers.start_openai()
    secret = "sk-live-must-never-be-logged"
    providers = build_registered_providers(
        {
            "providers": {
                "openai": {"api_key": secret, "base_url": _base_url(server)},
                "anthropic": {"api_key": secret, "base_url": _base_url(server)},
                "google": {"api_key": secret, "base_url": _base_url(server)},
                "github-copilot": {"api_key": secret, "base_url": _base_url(server)},
                "deepseek": {"api_key": secret, "base_url": _base_url(server)},
            }
        }
    )

    assert len(providers) > 1
    assert all(secret not in f"{provider!r}" for provider in providers)

    registered = {provider.name: provider for provider in providers}
    with pytest.raises(ProviderExecutionError) as excinfo:
        registered["deepseek"].propose_turn(_request(provider_name="ghost", model_name="ghost-model"))

    assert excinfo.value.kind == "not_configured"
    assert secret not in f"{excinfo.value.message} {excinfo.value.details} {excinfo.value!r}"
    assert server.requests == []


# ---------------------------------------------------------------------------
# Catalog pricing
# ---------------------------------------------------------------------------


def test_catalog_cost_is_attached_once_from_the_shipped_rates(servers: _Servers) -> None:
    server = servers.start_openai()
    server.replies.append(
        openai_response(
            message={"content": "done"},
            finish="stop",
            usage={"prompt_tokens": 1000, "completion_tokens": 500, "prompt_tokens_details": {"cached_tokens": 100}},
        )
    )
    provider = _provider({"zai": {"api_key": "loopback-key", "base_url": _base_url(server)}}, name="zai")

    usage = provider.propose_turn(_request(provider_name="zai", model_name="glm-4.5", raw_model="zai/glm-4.5")).usage

    assert usage is not None
    # 900 uncached * $0.6/M + 500 out * $2.2/M + 100 cache-read * $0.11/M
    assert usage.cost_usd == pytest.approx(0.001651)
    # Priced once: an already-priced usage is handed back untouched, never repriced.
    assert price_usage(provider_name="zai", model_name="glm-4.5", usage=usage) is usage
    assert price_usage(provider_name="anthropic", model_name="claude-opus-5", usage=usage) is usage


def test_a_model_without_a_shipped_rate_stays_unknown() -> None:
    usage = ProviderTokenUsage(input_tokens=1000, output_tokens=500, cache_read_tokens=100, uncached_input_tokens=900)

    priced = usage_cost_usd(
        usage=usage,
        metadata=static_catalog_metadata("deepseek", "deepseek-chat"),
    )

    assert priced is None
    assert price_usage(provider_name="deepseek", model_name="deepseek-chat", usage=usage).cost_usd is None


def test_a_tiered_row_cannot_price_a_wire_that_reports_no_cache_write_bucket(servers: _Servers) -> None:
    """The chat wire reports no cache-creation bucket, so a tiered cost stays unknown, never zero."""
    server = servers.start_openai()
    server.replies.append(
        openai_response(
            message={"content": "done"},
            finish="stop",
            usage={"prompt_tokens": 300000, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}},
        )
    )
    provider = _provider({"openai": {"api_key": "loopback-key", "base_url": _base_url(server)}}, name="openai")

    usage = provider.propose_turn(_request(provider_name="openai", model_name="gpt-5.6", raw_model="openai/gpt-5.6")).usage

    assert usage is not None
    assert usage.cache_write_tokens is None
    assert usage.cost_usd is None


def test_trace_write_failure_does_not_fail_a_completed_anthropic_request(servers: _Servers, tmp_path: Path, monkeypatch, caplog) -> None:
    server = servers.start_json()
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE", "1")
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE_LOG", str(tmp_path))
    provider = _provider({"anthropic": {"api_key": "loopback-key", "base_url": server.base_url}}, name="anthropic")
    try:
        result = provider.propose_turn(_request(provider_name="anthropic", model_name="claude-sonnet-4-5", raw_model="anthropic/claude-sonnet-4-5"))
        assert result.output == "anthropic-ok"
        assert result.done_reason == "stop"
        assert "Provider trace could not be written" in caplog.text
    finally:
        provider.close()


def test_google_timeout_bounds_the_real_sdk_request(servers: _Servers) -> None:
    server = servers.start_json(delay_seconds=1.0)
    provider = _provider({"google": {"api_key": "loopback-key", "base_url": server.base_url, "timeout_seconds": 0.1}}, name="google")
    started = time.monotonic()
    try:
        with pytest.raises(ProviderExecutionError) as caught:
            provider.propose_turn(_request(provider_name="google", model_name="gemini-2.5-flash", raw_model="google/gemini-2.5-flash"))
        assert caught.value.retryable is True
        assert caught.value.details["network_failure"] is True
        assert time.monotonic() - started < 0.8
        assert len(server.requests) == 1
    finally:
        provider.close()


@pytest.mark.parametrize("failed", [False, True])
def test_anthropic_tool_result_uses_the_sdk_content_and_error_flag(servers: _Servers, failed: bool) -> None:
    from voidcode.sdk.tools import ToolCall, ToolDefinition

    server = servers.start_json()
    provider = _provider({"anthropic": {"api_key": "loopback-key", "base_url": server.base_url}}, name="anthropic")
    request = _request(
        ProviderMessage("user", "inspect"),
        ProviderMessage("assistant", tool_calls=(ToolCall("inspect", {"path": "file.txt"}, tool_call_id="call-1"),)),
        ProviderMessage("tool", "tool-result-body", tool_call_id="call-1", tool_name="inspect", is_error=failed, metadata={"status": "error"}),
        provider_name="anthropic",
        model_name="claude-sonnet-4-5",
        raw_model="anthropic/claude-sonnet-4-5",
        available_tools=(ToolDefinition("inspect", "Inspect", {"type": "object", "properties": {"path": {"type": "string"}}}),),
    )
    try:
        assert provider.propose_turn(request).output == "anthropic-ok"
        payload = json.loads(server.requests[0].body)
        blocks = [block for message in payload["messages"] for block in message["content"]]
        result = next(block for block in blocks if block["type"] == "tool_result")
        assert result["tool_use_id"] == "call-1"
        assert result["content"] == "tool-result-body"
        assert result.get("is_error", False) is failed
        assert next(block for block in blocks if block["type"] == "tool_use")["input"] == {"path": "file.txt"}
    finally:
        provider.close()
