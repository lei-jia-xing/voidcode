"""Unit tests for the web plugin's ``web_fetch`` tool."""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable, Iterable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from voidcode.plugins.web import create_plugin
from voidcode.plugins.web.web_fetch import MAX_INLINE_TEXT_BYTES, MAX_RESPONSE_SIZE, WebFetchResultBody, WebFetchTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import AttachmentOutput, TextOutput, ToolCall, ToolResult

PUBLIC_URL = "https://93.184.216.34/docs"


class _DummyServices:
    """Kernel service bag stand-in: no ``create_artifact``, so output stays inline."""


class _CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self.chunks = iter(chunks)
        self.yielded = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    def close(self) -> None:
        pass


_SERVICES = cast(Any, _DummyServices())


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        session_id="s",
        run_id="r",
        invocation_id="i",
        revision_id="rev",
        actor="model",
        capabilities=frozenset({"workspace.read", "network"}),
    )


def _web_fetch_tool() -> WebFetchTool:
    return cast(WebFetchTool, {tool.definition.name: tool for tool in create_plugin({}).tools}["web_fetch"])


def _response(
    request: httpx.Request,
    *,
    content: bytes,
    content_type: str = "text/html; charset=utf-8",
    content_length: str | None = None,
    status_code: int = 200,
) -> httpx.Response:
    headers = {"Content-Type": content_type}
    if content_length is not None:
        headers["Content-Length"] = content_length
    return httpx.Response(status_code, headers=headers, content=content, request=request)


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    client_type = httpx.Client
    monkeypatch.setattr(
        "voidcode.plugins.web.web_fetch.httpx.Client",
        lambda *args, **kwargs: client_type(*args, transport=httpx.MockTransport(handler), **kwargs),
    )


def _fetch(context: ToolContext, *, url: str = PUBLIC_URL, output_format: str = "markdown") -> ToolResult:
    return _web_fetch_tool().invoke(
        ToolCall(tool_name="web_fetch", arguments={"url": url, "format": output_format}),
        context=context,
        services=_SERVICES,
    )


def test_webfetch_streams_html_into_markdown_without_artifact_reference(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"<html><body><h1>TITLE</h1><p>Hello</p></body></html>"
    requests: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(request, content=html)

    _mock_transport(monkeypatch, serve)
    result = _fetch(tool_context)

    assert isinstance(result.output, TextOutput)
    assert isinstance(result.body, WebFetchResultBody)
    assert result.output.text.endswith("# TITLE\n\nHello")
    assert len(requests) == 1
    assert requests[0].url == httpx.URL(PUBLIC_URL)
    payload = result.body.as_payload()
    assert payload["original_url"] == PUBLIC_URL
    assert payload["final_url"] == PUBLIC_URL
    assert payload["content_type"] == "text/html; charset=utf-8"
    encoding = payload["used_encoding"]
    assert isinstance(encoding, str) and encoding.lower() == "utf-8"
    assert payload["provenance"] == "web_fetch"
    assert "content" not in payload
    # ponytail: kernel artifact service (seam S5) is absent here, so the raw bytes are not
    # spilled: the reference is omitted and the bounded inline text is the only copy.
    assert result.output.bounds.reference is None
    assert result.output.bounds.truncated is False
    assert payload["source_artifact"] == {}
    assert "raw: unavailable" in result.output.text


def test_webfetch_truncates_inline_text_above_the_inline_bound(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_text = b"x" * (MAX_INLINE_TEXT_BYTES + 10)
    _mock_transport(monkeypatch, lambda request: _response(request, content=oversized_text, content_type="text/plain; charset=utf-8"))

    result = _fetch(tool_context, output_format="text")

    assert isinstance(result.output, TextOutput)
    assert isinstance(result.body, WebFetchResultBody)
    assert result.output.bounds.truncated is True
    assert result.output.bounds.reference is None
    assert f"truncated to {MAX_INLINE_TEXT_BYTES} bytes inline" in result.output.text
    assert result.body.byte_count == len(oversized_text)


def test_webfetch_format_selection_controls_extraction(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"<html><body><h1>TITLE</h1><p>Hello</p></body></html>"
    _mock_transport(monkeypatch, lambda request: _response(request, content=html))

    markdown = _fetch(tool_context, output_format="markdown")
    text = _fetch(tool_context, output_format="text")
    raw = _fetch(tool_context, output_format="html")

    assert isinstance(markdown.output, TextOutput) and isinstance(markdown.body, WebFetchResultBody)
    assert markdown.body.format == "markdown"
    assert markdown.output.text.endswith("# TITLE\n\nHello")

    assert isinstance(text.output, TextOutput) and isinstance(text.body, WebFetchResultBody)
    assert text.body.format == "text"
    assert "<h1>" not in text.output.text
    assert "TITLE" in text.output.text and "Hello" in text.output.text

    assert isinstance(raw.output, TextOutput) and isinstance(raw.body, WebFetchResultBody)
    assert raw.body.format == "html"
    assert raw.output.text.endswith(html.decode("utf-8"))

    with pytest.raises(ValueError, match="format must be"):
        _fetch(tool_context, output_format="pdf")


def test_webfetch_returns_real_attachment_data_uri_and_raw_body(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=")
    _mock_transport(
        monkeypatch,
        lambda request: _response(request, content=image_bytes, content_type="image/png"),
    )

    result = _fetch(tool_context)

    assert isinstance(result.output, AttachmentOutput)
    assert isinstance(result.body, WebFetchResultBody)
    assert result.output.mime == "image/png"
    presentation = result.output.presentation
    assert presentation is not None and presentation.endswith("Fetched image/png attachment.")
    assert result.output.data_uri == f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"
    # seam S5 again: attachment bytes reach the caller inline, never through an artifact reference.
    assert result.output.bounds.reference is None
    assert result.body.as_payload()["source_artifact"] == {}


def test_webfetch_treats_svg_as_text_not_raster_attachment(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>vector</text></svg>'
    _mock_transport(monkeypatch, lambda request: _response(request, content=svg, content_type="image/svg+xml; charset=utf-8"))
    result = _fetch(tool_context)

    assert isinstance(result.output, TextOutput)
    assert result.output.text.endswith(svg.decode("utf-8"))
    assert "MIME: image/svg+xml | charset: utf-8" in result.output.text


def test_webfetch_keeps_opaque_pdf_bytes_without_decoding(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = b"%PDF-opaque-\xff"
    _mock_transport(monkeypatch, lambda request: _response(request, content=pdf, content_type="application/pdf"))
    result = _fetch(tool_context)

    assert isinstance(result.output, TextOutput)
    assert "Opaque response bytes were preserved without text decoding." in result.output.text
    assert "MIME: application/pdf | charset: unspecified" in result.output.text
    assert isinstance(result.body, WebFetchResultBody)
    assert result.body.used_encoding is None
    assert result.body.byte_count == len(pdf)
    assert result.body.as_payload()["source_artifact"] == {}


@pytest.mark.parametrize("url", ["", "ftp://example.com", "http:///missing-host", "http://[x]/"])
def test_webfetch_rejects_urls_httpx_cannot_parse(tool_context: ToolContext, url: str) -> None:
    with pytest.raises(ValueError):
        _fetch(tool_context, url=url, output_format="text")


def test_webfetch_rejects_non_string_url(tool_context: ToolContext) -> None:
    with pytest.raises(ValueError, match="requires a string url argument"):
        _web_fetch_tool().invoke(
            ToolCall(tool_name="web_fetch", arguments={"url": None, "format": "text"}),
            context=tool_context,
            services=_SERVICES,
        )


def test_webfetch_reports_redirect_urls(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/docs":
            return httpx.Response(302, headers={"Location": "/final"}, request=request)
        return _response(request, content=b"final content", content_type="text/plain; charset=utf-8")

    _mock_transport(monkeypatch, redirect)
    result = _fetch(tool_context, output_format="html")

    assert isinstance(result.output, TextOutput)
    assert result.output.text.endswith("final content")
    assert len(requests) == 2
    assert isinstance(result.body, WebFetchResultBody)
    payload = result.body.as_payload()
    assert payload["original_url"] == PUBLIC_URL
    assert payload["final_url"] == "https://93.184.216.34/final"


def test_webfetch_rejects_too_many_redirects(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/again"}, request=request)

    _mock_transport(monkeypatch, redirect)
    with pytest.raises(ValueError, match="too many redirects"):
        _fetch(tool_context)
    assert len(requests) == 6


def test_webfetch_checks_content_length_before_reading_stream(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingStream([b"must not be read"])

    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Content-Length": str(MAX_RESPONSE_SIZE + 1)},
            stream=stream,
            request=request,
        )

    _mock_transport(monkeypatch, oversized)
    with pytest.raises(ValueError, match="Response too large"):
        _fetch(tool_context, output_format="text")
    assert stream.yielded == 0


def test_webfetch_stops_streaming_after_actual_byte_limit(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingStream([b"x" * 65536 for _ in range(MAX_RESPONSE_SIZE // 65536 + 2)])

    def chunked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream, request=request)

    _mock_transport(monkeypatch, chunked)
    with pytest.raises(ValueError, match="Response too large"):
        _fetch(tool_context, output_format="text")
    assert stream.yielded == MAX_RESPONSE_SIZE // 65536 + 1


def test_webfetch_uses_declared_non_utf8_encoding_without_replacement(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "“café”".encode("windows-1252")
    _mock_transport(
        monkeypatch,
        lambda request: _response(request, content=source, content_type="text/plain; charset=windows-1252"),
    )

    result = _fetch(tool_context, output_format="html")

    assert isinstance(result.output, TextOutput)
    assert isinstance(result.body, WebFetchResultBody)
    assert result.output.text.endswith("“café”")
    assert (result.body.as_payload()["used_encoding"] or "").lower() == "windows-1252"


def test_webfetch_rejects_malformed_declared_encoding_instead_of_returning_replacement_text(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_transport(
        monkeypatch,
        lambda request: _response(request, content=b"\xff", content_type="text/plain; charset=utf-8"),
    )

    with pytest.raises(ValueError, match="Failed to decode response"):
        _fetch(tool_context, output_format="html")


def test_webfetch_fetches_loopback_and_resolves_relative_redirect(
    tool_context: ToolContext,
) -> None:
    response_bytes = b"<html><body>loopback response</body></html>"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/start?"):
                self.send_response(302)
                self.send_header("Location", "/target?Keep=%2f")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(response_bytes)))
            self.end_headers()
            self.wfile.write(response_bytes)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_url = f"http://127.0.0.1:{server.server_port}/start?Case=%2f"
    try:
        result = _fetch(tool_context, url=original_url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert isinstance(result.body, WebFetchResultBody)
    payload = result.body.as_payload()
    assert payload["original_url"] == original_url
    assert payload["final_url"] == f"http://127.0.0.1:{server.server_port}/target?Keep=%2f"
    assert payload["byte_count"] == len(response_bytes)
    assert payload["source_artifact"] == {}
