"""Unit tests for the web plugin's ``web_fetch`` tool."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from voidcode.core.coverage import model_result_text
from voidcode.plugins.web import create_plugin
from voidcode.plugins.web.web_fetch import (
    CONTENT_REF_PREFIX,
    CONTINUATION_KIND,
    CONTINUATION_VERSION,
    MAX_ARTIFACT_PAGE_BYTES,
    MAX_RESPONSE_SIZE,
    WebFetchContinuationBody,
    WebFetchResultBody,
    WebFetchTool,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.limits import TOOL_OUTPUT_BYTES
from voidcode.sdk.services import ArtifactPage, ArtifactReference
from voidcode.sdk.tools import AttachmentOutput, TextOutput, ToolCall, ToolFailure, ToolResult, ToolSuccess

PUBLIC_URL = "https://93.184.216.34/docs"


class _DummyServices:
    """Core service bag stand-in: no artifact service, so output stays inline."""


class _MemoryServices:
    """Content-addressed artifact double matching the core's spill/read semantics."""

    def __init__(self) -> None:
        self.artifacts: dict[str, bytes] = {}
        self.reads = 0

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        sha256 = hashlib.sha256(payload).hexdigest()
        self.artifacts[sha256] = payload
        return ArtifactReference(sha256=sha256, bytes=len(payload), media_type=media_type)

    def read_artifact(
        self,
        reference: ArtifactReference | Mapping[str, object] | str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        self.reads += 1
        if isinstance(reference, ArtifactReference):
            sha256 = reference.sha256
        elif isinstance(reference, Mapping):
            sha256 = str(reference.get("sha256", ""))
        else:
            sha256 = str(reference)
        data = self.artifacts.get(sha256)
        if data is None:
            raise LookupError(f"unknown artifact {sha256}")
        page = data[offset : offset + limit]
        return ArtifactPage(
            sha256=sha256,
            offset=offset,
            total_bytes=len(data),
            data=page,
            eof=offset + len(page) >= len(data),
            media_type="",
        )


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


_REAL_CLIENT = httpx.Client


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    monkeypatch.setattr(
        "voidcode.plugins.web.web_fetch.httpx.Client",
        lambda *args, **kwargs: _REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs),
    )


def _invoke(context: ToolContext, arguments: Mapping[str, object], *, services: Any = _SERVICES) -> ToolResult:
    return _web_fetch_tool().invoke(
        ToolCall(tool_name="web_fetch", arguments=arguments),
        context=context,
        services=services,
    )


def _fetch(
    context: ToolContext,
    *,
    url: str = PUBLIC_URL,
    output_format: str = "markdown",
    services: Any = _SERVICES,
) -> ToolResult:
    return _invoke(context, {"url": url, "format": output_format}, services=services)


def _page_text(result: ToolResult) -> str:
    assert isinstance(result.output, TextOutput)
    head, separator, page = result.output.text.partition("\n\n")
    assert head and separator
    return page


def _continuation_body(result: ToolResult) -> WebFetchContinuationBody:
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, WebFetchContinuationBody)
    return result.body


def _initial_body(result: ToolResult) -> WebFetchResultBody:
    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, WebFetchResultBody)
    return result.body


def _manifest(services: _MemoryServices, content_ref: str) -> dict[str, object]:
    assert content_ref.startswith(CONTENT_REF_PREFIX)
    return cast(dict[str, object], json.loads(services.artifacts[content_ref[len(CONTENT_REF_PREFIX) :]]).copy())


def _rendered_bytes(services: _MemoryServices, content_ref: str) -> bytes:
    manifest = _manifest(services, content_ref)
    return services.artifacts[str(manifest["rendered_sha256"])]


def _collect_pages(services: _MemoryServices, context: ToolContext, first: ToolResult) -> tuple[str, list[str], int]:
    first_body = _initial_body(first)
    assert first_body.content_ref is not None
    pages = [_page_text(first)]
    next_offset = first_body.next_byte_offset
    total = first_body.total_bytes
    while next_offset is not None:
        page = _invoke(context, {"content_ref": first_body.content_ref, "byte_offset": next_offset}, services=services)
        body = _continuation_body(page)
        assert body.total_bytes == total
        pages.append(_page_text(page))
        next_offset = body.next_byte_offset
    return "".join(pages), pages, total


def _large_html() -> bytes:
    paragraphs = [f"<p>Paragraph {index} — 段落🙂 café " + "0123456789abcdef" * 8 + "</p>" for index in range(1200)]
    return ("<html><body>" + "".join(paragraphs) + "</body></html>").encode()


def test_webfetch_small_complete_page_stays_inline_without_artifacts(
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
    body = _initial_body(result)
    assert result.output.text.endswith("# TITLE\n\nHello")
    assert len(requests) == 1
    assert requests[0].url == httpx.URL(PUBLIC_URL)
    payload = body.as_payload()
    assert payload["original_url"] == PUBLIC_URL
    assert payload["final_url"] == PUBLIC_URL
    assert payload["content_type"] == "text/html; charset=utf-8"
    encoding = payload["used_encoding"]
    assert isinstance(encoding, str) and encoding.lower() == "utf-8"
    assert payload["provenance"] == "web_fetch"
    assert "content" not in payload
    # No service bag: no spill, no reference, and the whole small output is inline.
    assert result.output.bounds.reference is None
    assert result.output.bounds.truncated is False
    assert payload["source_artifact"] == {}
    assert payload["content_artifact"] == {}
    assert payload["content_ref"] is None
    assert payload["byte_offset"] == 0
    assert payload["next_byte_offset"] is None
    assert payload["eof"] is True
    assert payload["total_bytes"] == len("# TITLE\n\nHello")
    assert "raw: unavailable" in result.output.text
    assert "complete (eof)" in result.output.text


def test_webfetch_large_pages_reconstruct_exactly_and_spill_raw_and_rendered_once(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _large_html()
    services = _MemoryServices()
    _mock_transport(monkeypatch, lambda request: _response(request, content=html))

    result = _fetch(tool_context, services=services)
    body = _initial_body(result)

    # One spill each: raw response, rendered bytes, and the small continuation manifest.
    assert len(services.artifacts) == 3
    assert body.content_ref is not None
    assert body.truncated is True
    assert body.byte_offset == 0
    assert body.next_byte_offset == MAX_ARTIFACT_PAGE_BYTES
    assert body.total_bytes > MAX_ARTIFACT_PAGE_BYTES

    raw_sha = str(body.source_artifact["sha256"])
    assert services.artifacts[raw_sha] == html
    rendered = _rendered_bytes(services, body.content_ref)
    assert len(rendered) == body.total_bytes

    reconstructed, pages, _ = _collect_pages(services, tool_context, result)
    assert len(pages) > 1
    assert reconstructed.encode("utf-8") == rendered
    assert "\ufffd" not in reconstructed
    # The page text never rides in the structured body.
    assert "Paragraph 900" not in json.dumps(body.as_payload(), ensure_ascii=False)


def test_webfetch_manifest_is_opaque_and_describes_the_rendered_bytes(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _large_html()
    services = _MemoryServices()
    _mock_transport(monkeypatch, lambda request: _response(request, content=html))

    result = _fetch(tool_context, services=services)
    body = _initial_body(result)
    assert body.content_ref is not None
    manifest = _manifest(services, body.content_ref)

    assert body.content_ref == f"{CONTENT_REF_PREFIX}{body.content_artifact['sha256']}"
    assert manifest["kind"] == CONTINUATION_KIND
    assert manifest["version"] == CONTINUATION_VERSION
    assert manifest["format"] == "markdown"
    assert manifest["original_url"] == PUBLIC_URL
    assert manifest["final_url"] == PUBLIC_URL
    assert manifest["raw_sha256"] == body.source_artifact["sha256"]
    assert manifest["rendered_sha256"] == hashlib.sha256(_rendered_bytes(services, body.content_ref)).hexdigest()
    assert manifest["rendered_bytes"] == body.total_bytes


def test_webfetch_cursor_chain_reaches_eof_and_reports_it(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _large_html()
    services = _MemoryServices()
    _mock_transport(monkeypatch, lambda request: _response(request, content=html))

    result = _fetch(tool_context, services=services)
    body = _initial_body(result)
    assert body.content_ref is not None

    total = body.total_bytes
    cursor = body.next_byte_offset
    pages = 1
    last: WebFetchContinuationBody | None = None
    while cursor is not None:
        page = _invoke(tool_context, {"content_ref": body.content_ref, "byte_offset": cursor}, services=services)
        last = _continuation_body(page)
        assert isinstance(page.output, TextOutput)
        assert f"bytes {cursor}-" in page.output.text
        cursor = last.next_byte_offset
        pages += 1

    assert pages > 1
    assert last is not None
    assert last.eof is True
    assert last.truncated is False
    assert last.next_byte_offset is None
    assert last.total_bytes == total

    # The exact end offset is a valid empty EOF page; one byte past it is actionable failure.
    at_end = _invoke(tool_context, {"content_ref": body.content_ref, "byte_offset": total}, services=services)
    end_body = _continuation_body(at_end)
    assert end_body.eof is True and end_body.next_byte_offset is None
    assert _page_text(at_end) == ""

    beyond = _invoke(tool_context, {"content_ref": body.content_ref, "byte_offset": total + 1}, services=services)
    assert isinstance(beyond, ToolFailure)
    assert "past the end" in beyond.error


def test_webfetch_continuation_never_touches_the_network(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _large_html()
    services = _MemoryServices()
    _mock_transport(monkeypatch, lambda request: _response(request, content=html))
    body = _initial_body(_fetch(tool_context, services=services))
    assert body.content_ref is not None and body.next_byte_offset is not None

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("continuation must not construct an HTTP client")

    monkeypatch.setattr("voidcode.plugins.web.web_fetch.httpx.Client", no_network)
    page = _invoke(tool_context, {"content_ref": body.content_ref, "byte_offset": body.next_byte_offset}, services=services)

    assert isinstance(page, ToolSuccess)
    assert _continuation_body(page).byte_offset == body.next_byte_offset


def test_webfetch_utf8_boundary_split_keeps_exact_bytes_and_aligned_cursors(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "<p>" + 47100 ASCII + "é" places the 47104-byte page end inside the two-byte é.
    html = ("<p>" + "a" * 47100 + "é</p>").encode("utf-8")
    services = _MemoryServices()
    _mock_transport(monkeypatch, lambda request: _response(request, content=html))

    result = _fetch(tool_context, output_format="html", services=services)
    body = _initial_body(result)
    first_page = _page_text(result)
    assert body.next_byte_offset == 47103
    assert "é" not in first_page
    assert "\ufffd" not in first_page

    page = _invoke(tool_context, {"content_ref": body.content_ref or "", "byte_offset": body.next_byte_offset or 0}, services=services)
    continuation = _continuation_body(page)
    assert _page_text(page).startswith("é</p>")
    assert continuation.eof is True
    assert _page_text(result) + _page_text(page) == html.decode("utf-8")


def test_webfetch_strict_branches_reject_cross_branch_and_unknown_fields(tool_context: ToolContext) -> None:
    ref = f"{CONTENT_REF_PREFIX}{'0' * 64}"
    rejected = [
        {"url": PUBLIC_URL, "content_ref": ref},
        {"content_ref": ref, "byte_offset": 0, "url": PUBLIC_URL},
        {"content_ref": ref, "byte_offset": 0, "format": "text"},
        {"url": PUBLIC_URL, "unexpected": True},
        {"url": PUBLIC_URL, "format": "pdf"},
        {"url": PUBLIC_URL, "timeout": "fast"},
        {"url": PUBLIC_URL, "timeout": 0},
        {"content_ref": ref},
        {"byte_offset": 0},
        {"content_ref": ref, "byte_offset": "0"},
        {"content_ref": ref, "byte_offset": -1},
        {},
    ]
    for arguments in rejected:
        with pytest.raises(ValueError, match="Validation error"):
            _invoke(tool_context, arguments)


def test_webfetch_rejects_non_string_url(tool_context: ToolContext) -> None:
    with pytest.raises(ValueError, match="Validation error"):
        _invoke(tool_context, {"url": None, "format": "text"})


def test_webfetch_continuation_rejects_bad_refs_unknown_artifacts_and_malformed_manifests(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _MemoryServices()
    _mock_transport(monkeypatch, lambda request: _response(request, content=_large_html()))
    body = _initial_body(_fetch(tool_context, services=services))
    assert body.content_ref is not None

    bad_refs = [
        f"workspace://artifact/{'0' * 64}",
        "voidcode://artifact/not-a-sha",
        f"{CONTENT_REF_PREFIX}{'0' * 64}",
        body.source_artifact["uri"],
    ]
    for bad_ref in bad_refs:
        result = _invoke(tool_context, {"content_ref": bad_ref, "byte_offset": 0}, services=services)
        assert isinstance(result, ToolFailure)
        assert result.error.startswith("web_fetch continuation failed:")

    malformed = services.spill_artifact(json.dumps({"kind": "something_else", "version": 1}), media_type="application/json")
    result = _invoke(tool_context, {"content_ref": f"{CONTENT_REF_PREFIX}{malformed.sha256}", "byte_offset": 0}, services=services)
    assert isinstance(result, ToolFailure)
    assert CONTINUATION_KIND in result.error


def test_webfetch_continuation_rejects_offsets_inside_a_multibyte_character(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _MemoryServices()
    # é occupies bytes 47102-47103, so 47103 is its continuation byte.
    split_html = ("<p>" + "a" * 47099 + "é</p>").encode("utf-8")
    _mock_transport(monkeypatch, lambda request: _response(request, content=split_html))
    body = _initial_body(_fetch(tool_context, output_format="html", services=services))
    assert body.content_ref is not None

    result = _invoke(tool_context, {"content_ref": body.content_ref, "byte_offset": 47103}, services=services)
    assert isinstance(result, ToolFailure)
    assert "UTF-8 character boundary" in result.error


def test_webfetch_invalid_url_and_transport_errors_are_safe_tool_failures(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for url in ["", "ftp://example.com", "http:///missing-host", "http://[x]/"]:
        result = _invoke(tool_context, {"url": url, "format": "text"})
        assert isinstance(result, ToolFailure)
        assert "web_fetch failed" in result.error

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("internal-socket-detail")

    _mock_transport(monkeypatch, broken)
    result = _fetch(tool_context, output_format="text")
    assert isinstance(result, ToolFailure)
    assert "ReadError" in result.error
    assert "internal-socket-detail" not in result.error

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("internal-timeout-detail")

    _mock_transport(monkeypatch, timeout)
    result = _fetch(tool_context, output_format="text")
    assert isinstance(result, ToolFailure)
    assert "timed out" in result.error
    assert "internal-timeout-detail" not in result.error


def test_webfetch_http_404_is_actionable_through_the_model_projection(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_transport(
        monkeypatch,
        lambda request: _response(request, content=b"missing", content_type="text/plain", status_code=404),
    )

    result = _fetch(tool_context, output_format="text")

    assert isinstance(result, ToolFailure)
    projected, clipped = model_result_text(result)
    assert projected is not None and clipped is False
    assert "HTTP 404" in projected
    assert "web_search" in projected
    assert "Traceback" not in projected
    assert result.body is None


def test_webfetch_projected_pages_stay_within_the_model_bound(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _large_html()
    services = _MemoryServices()
    _mock_transport(monkeypatch, lambda request: _response(request, content=html))
    result = _fetch(tool_context, services=services)
    body = _initial_body(result)

    initial_text, initial_clipped = model_result_text(result)
    assert initial_text is not None and initial_clipped is False
    assert len(initial_text.encode("utf-8")) <= TOOL_OUTPUT_BYTES

    assert body.content_ref is not None and body.next_byte_offset is not None
    page = _invoke(tool_context, {"content_ref": body.content_ref, "byte_offset": body.next_byte_offset}, services=services)
    continuation_text, continuation_clipped = model_result_text(page)
    assert continuation_text is not None and continuation_clipped is False
    assert len(continuation_text.encode("utf-8")) <= TOOL_OUTPUT_BYTES


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

    with pytest.raises(ValueError, match="format"):
        _fetch(tool_context, output_format="pdf")


def test_webfetch_returns_real_attachment_data_uri_and_raw_body(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=")
    services = _MemoryServices()
    _mock_transport(
        monkeypatch,
        lambda request: _response(request, content=image_bytes, content_type="image/png"),
    )

    result = _fetch(tool_context, services=services)

    assert isinstance(result.output, AttachmentOutput)
    assert isinstance(result.body, WebFetchResultBody)
    assert result.output.mime == "image/png"
    presentation = result.output.presentation
    assert presentation is not None and presentation.endswith("Fetched image/png attachment.")
    assert result.output.data_uri == f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"
    assert result.output.bounds.reference is not None
    assert result.output.bounds.reference.uri == result.body.source_artifact["uri"]
    assert result.body.as_payload()["content_artifact"] == {}
    assert result.body.content_ref is None
    assert result.body.truncated is False


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
    body = _initial_body(result)
    assert body.used_encoding is None
    assert body.byte_count == len(pdf)
    assert body.as_payload()["source_artifact"] == {}
    assert body.content_ref is None
    assert body.eof is True


def test_webfetch_truncates_inline_text_when_no_artifact_service_is_available(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_text = b"<html><body>" + b"<p>" + b"x" * (MAX_ARTIFACT_PAGE_BYTES + 10) + b"</p></body></html>"
    _mock_transport(monkeypatch, lambda request: _response(request, content=oversized_text))

    result = _fetch(tool_context, output_format="text")

    assert isinstance(result.output, TextOutput)
    body = _initial_body(result)
    assert result.output.bounds.truncated is True
    assert result.output.bounds.reference is None
    assert body.content_artifact == {}
    assert body.content_ref is None
    assert body.next_byte_offset is None
    assert body.eof is False
    assert body.truncated is True
    assert body.total_bytes > MAX_ARTIFACT_PAGE_BYTES
    assert "continuation unavailable" in result.output.text
    assert body.byte_count == len(oversized_text)


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
    body = _initial_body(result)
    payload = body.as_payload()
    assert payload["original_url"] == PUBLIC_URL
    assert payload["final_url"] == "https://93.184.216.34/final"


def test_webfetch_rejects_too_many_redirects(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/again"}, request=request)

    _mock_transport(monkeypatch, redirect)
    result = _fetch(tool_context)
    assert isinstance(result, ToolFailure)
    assert "redirects" in result.error
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
    result = _fetch(tool_context, output_format="text")
    assert isinstance(result, ToolFailure)
    assert "exceeds the 5 MiB" in result.error
    assert stream.yielded == 0


def test_webfetch_stops_streaming_after_actual_byte_limit(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingStream([b"x" * 65536 for _ in range(MAX_RESPONSE_SIZE // 65536 + 2)])

    def chunked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=stream, request=request)

    _mock_transport(monkeypatch, chunked)
    result = _fetch(tool_context, output_format="text")
    assert isinstance(result, ToolFailure)
    assert "exceeds the 5 MiB" in result.error
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
    body = _initial_body(result)
    assert result.output.text.endswith("“café”")
    used_encoding = body.as_payload()["used_encoding"]
    assert isinstance(used_encoding, str) and used_encoding.lower() == "windows-1252"


def test_webfetch_rejects_malformed_declared_encoding_with_safe_failure(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_transport(
        monkeypatch,
        lambda request: _response(request, content=b"\xff", content_type="text/plain; charset=utf-8"),
    )

    result = _fetch(tool_context, output_format="html")
    assert isinstance(result, ToolFailure)
    assert "declared charset" in result.error
    assert "Traceback" not in result.error


def test_webfetch_fetches_loopback_and_resolves_relative_redirect(
    tool_context: ToolContext,
) -> None:
    response_bytes = b"<html><body>loopback response</body></html>"
    services = _MemoryServices()

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
        result = _fetch(tool_context, url=original_url, services=services)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    body = _initial_body(result)
    payload = body.as_payload()
    assert payload["original_url"] == original_url
    assert payload["final_url"] == f"http://127.0.0.1:{server.server_port}/target?Keep=%2f"
    assert payload["byte_count"] == len(response_bytes)
    assert payload["source_artifact"] != {}
    assert body.eof is True
    assert result.output.bounds.reference is not None
