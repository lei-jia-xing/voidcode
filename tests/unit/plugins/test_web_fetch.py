"""Behavioral tests for the web plugin's ``web_fetch`` tool."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx
import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.bootstrap import open_core
from voidcode.core.coverage import project_tool_result
from voidcode.plugins.web import create_plugin
from voidcode.plugins.web.web_fetch import FETCH_MAX_BYTES, WebFetchResultBody, WebFetchTool
from voidcode.plugins.web.web_reader import read_web_snapshot
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.resources import ResourceReadCaller, ResourceReadRequest
from voidcode.sdk.tools import EmptyOutput, OpaqueToolBody, ToolCall, ToolFailure, ToolResult, ToolSuccess

_HTML = (
    '<html><head><title>Smoke</title></head><body><h1>Hello fetch</h1><p>Local <a href="/other">link</a>.</p><script>alert(1)</script></body></html>'
)


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        session_id="s",
        run_id="r",
        invocation_id="i",
        revision_id="rev",
        actor="model",
    )


class _AbortSignal:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.reason: str | None = None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self.cancelled = value
        self.reason = reason if value else None


def _tool() -> WebFetchTool:
    return cast(WebFetchTool, {tool.definition.name: tool for tool in create_plugin({}).tools}["web_fetch"])


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    client_type = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "voidcode.plugins.web.web_fetch.httpx.Client",
        lambda *args, **kwargs: client_type(*args, transport=transport, **kwargs),
    )


def _run(context: ToolContext, services: FakeServices, arguments: dict[str, object]) -> ToolResult:
    return _tool().invoke(ToolCall(tool_name="web_fetch", arguments=arguments), context=context, services=cast(Any, services))


def _ok(context: ToolContext, services: FakeServices, url: str) -> ToolSuccess[WebFetchResultBody]:
    result = _run(context, services, {"url": url})
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, WebFetchResultBody)
    return result


def test_webfetch_admitted_runtime_serves_snapshot_then_survives_web_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One consumer regression: fetch, read web:// + continuation, then re-admit without web."""
    long_text = ("snapshot line\n" * 3000).strip()

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://example.com/long":
            return httpx.Response(200, content=long_text.encode(), headers={"content-type": "text/plain"}, request=request)
        return httpx.Response(200, content=_HTML.encode(), headers={"content-type": "text/html; charset=utf-8"}, request=request)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        # Patch after admission: the mock replaces the module-level Client factory.
        _mock_transport(monkeypatch, handle)
        session = core.create_session("web-fetch-runtime")
        runtime = core.plugin_runtime
        fetch_def = next(d for d in runtime.tools if d.name == "web_fetch")
        assert {e.value for e in fetch_def.effects} == {"read", "network"}
        assert fetch_def.effective_replay_policy == "never"
        context = ToolContext(
            workspace=workspace,
            session_id=session.session_id,
            run_id="r",
            invocation_id="i",
            revision_id=runtime.revision_id,
            actor="model",
        )
        fetched = runtime.invoke(ToolCall("web_fetch", {"url": "http://example.com/page"}), context=context)
        assert isinstance(fetched, ToolSuccess)
        assert fetched.coverage == ()
        fetched_payload = fetched.body.as_payload() if fetched.body is not None else {}
        assert fetched_payload["url"] == "http://example.com/page"
        assert fetched_payload["final_url"] == "http://example.com/page"
        assert fetched_payload["uri"].startswith("web://")
        read_back = runtime.invoke(ToolCall("read", {"path": fetched_payload["uri"]}), context=context)
        assert isinstance(read_back, ToolSuccess)
        assert read_back.coverage == ()
        assert "Hello fetch" in str(read_back.output.text)
        assert "http://example.com/other" in str(read_back.output.text)
        canonical_uri = f"artifact://{fetched_payload['artifact']['sha256']}"
        canonical = runtime.invoke(ToolCall("read", {"path": canonical_uri}), context=context)
        assert isinstance(canonical, ToolSuccess)
        assert canonical.coverage == ()
        assert "Hello fetch" in str(canonical.output.text)
        long_fetch = runtime.invoke(ToolCall("web_fetch", {"url": "http://example.com/long"}), context=context)
        assert isinstance(long_fetch, ToolSuccess)
        long_payload = long_fetch.body.as_payload() if long_fetch.body is not None else {}
        first = runtime.invoke(ToolCall("read", {"path": long_payload["uri"]}), context=context)
        assert isinstance(first, ToolSuccess) and isinstance(first.output.text, str)
        assert first.output.bounds.truncated is True
        assert isinstance(first.body, OpaqueToolBody)
        payload = first.body.as_payload()
        continued = runtime.invoke(ToolCall("read", {"path": long_payload["uri"], "offset": payload["next_offset"]}), context=context)
        assert isinstance(continued, ToolSuccess)
        stored_digest = fetched_payload["artifact"]["sha256"]
        monkeypatch.undo()
        (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": {"web": {"enabled": False}}}), encoding="utf-8")
        reloaded = core.invoke_command(session.session_id, "core.reload", {}, expected_revision=core.command_catalog().revision_id)
        assert reloaded.status == "ok", reloaded.error
        assert "web_fetch" not in {d.name for d in core.plugin_runtime.tools}
        context = ToolContext(
            workspace=workspace,
            session_id=session.session_id,
            run_id="r2",
            invocation_id="i2",
            revision_id=core.plugin_runtime.revision_id,
            actor="model",
        )
        # web:// is gone with its owner, but the canonical bytes survive under artifact://.
        gone = core.plugin_runtime.invoke(ToolCall("read", {"path": f"web://{stored_digest}"}), context=context)
        assert isinstance(gone, ToolFailure)
        assert "unsupported resource URI" in gone.error
        recovered = core.plugin_runtime.invoke(ToolCall("read", {"path": f"artifact://{stored_digest}"}), context=context)
        assert isinstance(recovered, ToolSuccess)
        assert "Hello fetch" in str(recovered.output.text)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"url": 123},
        {"url": "http://example.com", "extra": True},
        {"url": ""},
        {"url": "   "},
        {"url": "ftp://example.com/page"},
        {"url": "http://user:pass@example.com/"},
        {"url": "http:///no-host"},
        {"url": "not a url at all http://"},
        {"url": "http://example.com/\n"},
        {"url": "http://example.com/\x01"},
        {"url": "http://example.com/\x7f"},
        {"url": "http://[::1"},
    ],
)
def test_webfetch_rejects_bad_input_before_http(tool_context: ToolContext, arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _run(tool_context, FakeServices(), arguments)


def test_webfetch_rejects_credentials_without_echo(tool_context: ToolContext) -> None:
    with pytest.raises(ValueError) as excinfo:
        _run(tool_context, FakeServices(), {"url": "https://user:s3cret@example.com/"})
    assert "s3cret" not in str(excinfo.value)


def test_webfetch_renders_html_to_markdown_with_usable_links_and_full_artifact(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML.encode(), headers={"content-type": "text/html; charset=utf-8"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _ok(tool_context, services, "http://example.com/page")

    body = result.body
    assert body.url == "http://example.com/page"
    assert body.final_url == "http://example.com/page"
    assert body.content_type == "text/html"
    assert body.uri == f"web://{body.artifact.sha256}"
    assert body.artifact.media_type == "text/markdown; charset=utf-8"
    stored = services.artifacts[body.artifact.sha256]
    full = stored.decode("utf-8")
    assert len(stored) == body.bytes
    assert body.artifact.sha256 == hashlib.sha256(stored).hexdigest()
    # Scripts are dropped, relative links resolve against the final URL.
    assert "alert(1)" not in full
    assert "http://example.com/other" in full
    # Complete bytes live only behind a typed reference, not in the event presentation.
    assert isinstance(result.output, EmptyOutput)
    assert result.output.presentation == "Fetched http://example.com/page."
    assert result.output.bounds.reference == body.artifact
    assert result.output.bounds.source == "http://example.com/page"
    assert "alert(1)" not in str(body.as_payload())


def test_webfetch_follows_redirect_and_resolves_links_against_final_url(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == "http://example.com/start":
            return httpx.Response(302, headers={"location": "/page"}, request=request)
        return httpx.Response(200, content=_HTML.encode(), headers={"content-type": "text/html"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _ok(tool_context, services, "http://example.com/start")

    assert seen == ["http://example.com/start", "http://example.com/page"]
    assert result.body.final_url == "http://example.com/page"
    assert result.output.bounds.source == "http://example.com/page"
    full = services.artifacts[result.body.artifact.sha256].decode("utf-8")
    assert "http://example.com/other" in full


def test_webfetch_rejects_non_redirectable_scheme(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "ftp://evil.example/x"}, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/start"})

    assert isinstance(result, ToolFailure)
    assert "redirect" in result.error


def test_webfetch_rejects_redirect_with_credentials(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://user:pw@example.com/"}, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/start"})

    assert isinstance(result, ToolFailure)
    assert "pw" not in result.error


def test_webfetch_rejects_redirect_location_with_controls(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers=[(b"location", b"http://example.com/\x01")], request=request)

    _mock_transport(monkeypatch, handle)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/start"})

    assert isinstance(result, ToolFailure)
    assert "malformed response or redirect" in result.error
    assert seen == ["http://example.com/start"]


def test_webfetch_enforces_decompressed_cap_not_content_length(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    big = b"x" * (FETCH_MAX_BYTES + 1024)
    payload = gzip.compress(big)

    def handle(request: httpx.Request) -> httpx.Response:
        # Content-Length stays small; the decompressed stream exceeds the cap.
        return httpx.Response(200, content=payload, headers={"content-type": "text/plain", "content-encoding": "gzip"}, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/big"})

    assert isinstance(result, ToolFailure)
    assert "byte limit" in result.error


def test_webfetch_reports_http_status(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"missing", headers={"content-type": "text/plain"}, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/gone"})

    assert isinstance(result, ToolFailure)
    assert "HTTP 404" in result.error


def test_webfetch_rejects_binary_type_without_decoding(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _run(tool_context, services, {"url": "http://example.com/pic.png"})

    assert isinstance(result, ToolFailure)
    assert "image/png" in result.error
    assert services.artifacts == {}


def test_webfetch_decodes_declared_charset(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    body_bytes = "café naïve".encode("latin-1")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body_bytes, headers={"content-type": "text/plain; charset=latin-1"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _ok(tool_context, services, "http://example.com/latin")

    full = services.artifacts[result.body.artifact.sha256].decode("utf-8")
    assert "café naïve" in full
    assert result.body.artifact.media_type == "text/plain; charset=utf-8"


def test_webfetch_rejects_undecodable_body_without_leaking_bytes(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\x00bad", headers={"content-type": "text/plain; charset=utf-8"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _run(tool_context, services, {"url": "http://example.com/broken"})

    assert isinstance(result, ToolFailure)
    assert "not valid utf-8" in result.error
    assert services.artifacts == {}


def test_webfetch_rejects_non_text_charset(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"aGVsbG8=", headers={"content-type": "text/plain; charset=base64"}, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/encoded"})

    assert isinstance(result, ToolFailure)
    assert "non-text charset" in result.error


@pytest.mark.parametrize("charset", ["unicode_escape", "raw_unicode_escape"])
def test_webfetch_rejects_escape_codecs_before_surrogate_encode(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch, charset: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\\ud800", headers={"content-type": f"text/plain; charset={charset}"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _run(tool_context, services, {"url": "http://example.com/surrogate"})

    assert isinstance(result, ToolFailure)
    assert "non-text charset" in result.error
    assert services.artifacts == {}


class _RejectingArtifactServices(FakeServices):
    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> NoReturn:
        raise ValueError("artifact media_type must be a short printable string")


def test_webfetch_bounds_artifact_store_rejection(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"}, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run(tool_context, _RejectingArtifactServices(), {"url": "http://example.com/page"})

    assert isinstance(result, ToolFailure)
    assert "could not store the response" in result.error
    assert "media_type" not in result.error


def test_webfetch_bounds_unparsable_request_url(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_invalid(*args: object, **kwargs: object) -> None:
        raise httpx.InvalidURL("unparsable")

    monkeypatch.setattr(httpx.Client, "stream", raise_invalid)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/page"})

    assert isinstance(result, ToolFailure)
    assert "could not parse the request URL" in result.error


def test_webfetch_supports_json_body(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hello": "world"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _ok(tool_context, services, "http://example.com/data.json")

    assert result.body.content_type == "application/json"
    full = services.artifacts[result.body.artifact.sha256].decode("utf-8")
    assert "hello" in full


def test_webfetch_returns_complete_document_reference_and_host_projects_a_page(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = "lorem ipsum dolor sit amet " * 2000

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=long_text.encode(), headers={"content-type": "text/plain"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _ok(tool_context, services, "http://example.com/long")

    from dataclasses import replace

    from voidcode.sdk.codec import encode_tool_result

    assert isinstance(result.output, EmptyOutput)
    assert result.output.bounds.reference == result.body.artifact
    full = services.artifacts[result.body.artifact.sha256].decode("utf-8")
    assert full == long_text
    assert result.output.presentation is not None and long_text not in result.output.presentation
    canonical = services.spill_artifact(json.dumps(encode_tool_result(result)), media_type="application/json")
    projected, _, clipped = project_tool_result(
        replace(result, result_reference=canonical),
        max_bytes=2048,
        artifact_reader=lambda digest, offset, limit: services.read_artifact(digest, offset=offset, limit=limit),
    )
    assert clipped and len(projected.encode("utf-8")) <= 2048
    assert result.body.uri in projected and result.body.artifact.uri in projected
    assert json.loads(projected)["text"].startswith("lorem ipsum")


def test_webfetch_cancelled_before_request(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"nope", headers={"content-type": "text/plain"}, request=request)

    _mock_transport(monkeypatch, handle)
    context = ToolContext(
        workspace=tool_context.workspace,
        session_id="s",
        run_id="r",
        invocation_id="i",
        revision_id="rev",
        actor="model",
        abort_signal=cast(Any, _AbortSignal(cancelled=True)),
    )
    result = _run(context, FakeServices(), {"url": "http://example.com/page"})

    assert isinstance(result, ToolFailure)
    assert "cancelled" in result.error
    assert calls == []


def test_webfetch_bounds_transport_errors(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("internal-socket-detail")

    _mock_transport(monkeypatch, fail)
    result = _run(tool_context, FakeServices(), {"url": "http://example.com/page"})

    assert isinstance(result, ToolFailure)
    assert "internal-socket-detail" not in result.error


def test_web_reader_rejects_malformed_digest(tool_context: ToolContext) -> None:
    services = FakeServices()
    caller = ResourceReadCaller(session_id="s", workspace=tool_context.workspace, revision_id="rev", run_id="r", abort_signal=None)
    with pytest.raises(ValueError):
        read_web_snapshot(ResourceReadRequest(uri="web://not-a-digest"), caller, cast(Any, services))


def test_web_reader_returns_none_for_unknown_digest(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    services = FakeServices()
    caller = ResourceReadCaller(session_id="s", workspace=tool_context.workspace, revision_id="rev", run_id="r", abort_signal=None)

    def _missing(sha256: str, *, offset: int = 0, limit: int = 65536) -> object:
        raise FileNotFoundError(f"artifact is not stored: {sha256}")

    monkeypatch.setattr(services, "read_artifact", _missing)
    assert read_web_snapshot(ResourceReadRequest(uri=f"web://{'0' * 64}"), caller, cast(Any, services)) is None


@pytest.mark.parametrize(
    "restricted_url",
    [
        "http://127.0.0.1/admin",
        "http://127.1.2.3:8080/",
        "http://[::1]/",
        "http://localhost:3000/",
        "http://10.0.0.5/",
        "http://192.168.1.1/router",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_webfetch_rejects_ssrf_restricted_targets(tool_context: ToolContext, restricted_url: str) -> None:
    services = FakeServices()
    with pytest.raises(Exception) as excinfo:
        _run(tool_context, services, {"url": restricted_url})
    assert "restricted/private network address" in str(excinfo.value)


def test_webfetch_rejects_ssrf_redirect_target(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"}, request=request)

    _mock_transport(monkeypatch, handle)
    services = FakeServices()
    result = _run(tool_context, services, {"url": "http://example.com/redirect"})
    assert isinstance(result, ToolFailure)
    assert "redirect target must be an http(s) URL without credentials" in result.error
