"""Consumer regressions for complete results, bounded projections, and real image/provider passage."""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import sys
import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import CancelledError
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from tests.fixtures.core_factory import loopback_server
from tests.fixtures.png import decode_rgb_png, rgb_png
from tests.fixtures.s5_plugin import PLUGIN_ID, PROBE_TOOL, write_s5_plugin
from tests.unit.core.test_nested_tools import _call, _turn
from tests.unit.plugins.test_providers_auth_catalog import _JsonLoopback, _provider, _request
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.capabilities.files.read_tool import ReadTool
from voidcode.core.artifacts import MAX_PAGE_BYTES, ArtifactNotFoundError, ArtifactStore
from voidcode.core.coverage import MODEL_PROJECTION_MARKER, ProjectionRequiresReference, project_tool_result, read_artifact_content
from voidcode.core.services import RuntimeServices
from voidcode.sdk.codec import decode_tool_result, encode_tool_result
from voidcode.sdk.hooks import InvocationFinishedFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ImageAttachment, ProviderMessage
from voidcode.sdk.services import ArtifactPage, ArtifactReference, PluginServices
from voidcode.sdk.tools import (
    TOOL_OUTPUT_BYTES,
    AttachmentOutput,
    EmptyOutput,
    OpaqueToolBody,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDiagnostics,
    ToolFailure,
    ToolSuccess,
)


class _ArtifactServices(PluginServices):
    def __init__(self, root: Path) -> None:
        self.store = ArtifactStore(root)
        self.reads: list[tuple[int, int]] = []

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        return self.spill_artifact_stream((data.encode() if isinstance(data, str) else data,), media_type=media_type)

    def spill_artifact_stream(self, chunks: Iterable[bytes], *, media_type: str = "application/octet-stream") -> ArtifactReference:
        identity, spill = self.store.start_spill(media_type, owner="test")
        try:
            for chunk in chunks:
                spill.append(chunk)
            digest, size, media = self.store.finish(identity)
        except BaseException:
            self.store.discard(identity)
            raise
        return ArtifactReference(digest, size, media)

    def page(self, digest: str, offset: int, limit: int) -> ArtifactPage:
        self.reads.append((offset, limit))
        data, total, eof, media = self.store.read(digest, offset=offset, limit=limit)
        return ArtifactPage(ArtifactReference(digest, total, media), offset, data, eof)


def _canonical(services: _ArtifactServices, result):
    original = replace(result, result_reference=None)
    reference = services.spill_artifact(
        json.dumps(encode_tool_result(original), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        media_type="application/vnd.voidcode.tool-result+json",
    )
    return replace(original, result_reference=reference)


def _configure(tmp_path: Path, *, provider: str, base_url: str, extra_plugins: dict[str, object] | None = None) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    plugins = {identity: {"enabled": identity in {"providers", "files", "exec"}} for identity in BUNDLED_PLUGIN_IDS}
    plugins.update(extra_plugins or {})
    model = "claude-sonnet-4-5" if provider == "anthropic" else "exact-model"
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": f"{provider}/{model}"},
                "policy": {"approval_mode": "yolo"},
                "providers": {provider: {"api_key": "image-loopback-secret", "base_url": base_url}},
                "plugins": plugins,
            }
        ),
        encoding="utf-8",
    )
    return workspace


def test_selected_text_is_authoritative_and_machine_body_remains_recoverable(tmp_path: Path) -> None:
    services = _ArtifactServices(tmp_path)
    raw = ToolSuccess("inspect", TextOutput("selected model data"), OpaqueToolBody({"raw": "required business field"}))
    with pytest.raises(ProjectionRequiresReference):
        project_tool_result(raw, max_bytes=1024)
    result = _canonical(services, raw)
    text, images, content_clipped = project_tool_result(result, max_bytes=1024)
    document = json.loads(text)
    assert "body" not in document and document["body_omitted"]
    assert document["text"] == "selected model data" and "text_partial" not in document
    assert document["result_uri"] == result.result_reference.uri
    assert images == () and not content_clipped
    original = json.loads(read_artifact_content(result.result_reference, services.page))
    assert original["output"]["text"] == "selected model data"
    assert original["body"] == {"raw": "required business field"}
    assert "result_reference" not in original


def test_structured_only_empty_output_keeps_its_body_inline() -> None:
    result = ToolSuccess("inspect", EmptyOutput(), OpaqueToolBody({"rows": [{"id": 7}]}))
    text, _, content_clipped = project_tool_result(result, max_bytes=1024)
    assert json.loads(text)["body"] == {"rows": [{"id": 7}]}
    assert not content_clipped


def test_long_error_unicode_and_metadata_never_exceed_projection_allocation(tmp_path: Path) -> None:
    services = _ArtifactServices(tmp_path)
    result = _canonical(
        services,
        ToolFailure(
            "query",
            "原因🎯" * 4000 + "late root cause",
            TextOutput("partial text", bounds=OutputBounds(source="query.sql")),
            OpaqueToolBody({"rows": ["row"] * 4000}),
            diagnostics=ToolDiagnostics(summary="d" * 8000, guidance="read the full result"),
        ),
    )
    text, _, content_clipped = project_tool_result(result, max_bytes=1024)
    document = json.loads(text)
    assert not content_clipped and len(text.encode()) <= 1024
    assert document["text"] == "partial text" and "text_partial" not in document
    assert document["status"] == "error" and document["error_partial"]
    assert document["projection_note"] == MODEL_PROJECTION_MARKER
    assert document["result_uri"] == result.result_reference.uri
    assert document["body_omitted"] and document["diagnostics_omitted"]
    assert "late root cause" in json.loads(read_artifact_content(result.result_reference, services.page))["error"]
    with pytest.raises(ValueError, match="cannot fit"):
        project_tool_result(result, max_bytes=32)


def test_reference_text_pages_only_explicit_out_of_line_empty_output(tmp_path: Path) -> None:
    services = _ArtifactServices(tmp_path)
    output = services.spill_artifact("visible start\n" + "后续完整文本\n" * 100000)
    result = _canonical(services, ToolSuccess("capture", EmptyOutput("captured document", bounds=OutputBounds(reference=output))))
    text, _, clipped = project_tool_result(result, max_bytes=2048, artifact_reader=services.page)
    assert clipped and len(text.encode()) <= 2048
    document = json.loads(text)
    assert document["text"].startswith("visible start\n")
    assert document["presentation"] == "captured document"
    assert document["output_uri"] == output.uri and document["result_uri"] == result.result_reference.uri
    assert sum(limit for _, limit in services.reads) <= 2048
    assert all(limit <= MAX_PAGE_BYTES for _, limit in services.reads)

    prior_reads = list(services.reads)
    page_path = tmp_path / "page.txt"
    page_path.write_text("first-line\n" + "x" * 3000 + "\nlast-line\n", encoding="utf-8")
    selected_page = ReadTool().invoke(
        ToolCall("read", {"path": page_path.name, "offset": 2, "limit": 1}),
        context=ToolContext(workspace=tmp_path, session_id="s", run_id="r", invocation_id="i", revision_id="v", actor="model"),
        services=services,
    )
    assert isinstance(selected_page, ToolSuccess) and isinstance(selected_page.output, TextOutput)
    assert isinstance(selected_page.output.bounds.reference, ArtifactReference)
    selected_page = _canonical(services, selected_page)
    projected, _, selected_clipped = project_tool_result(selected_page, max_bytes=2048, artifact_reader=services.page)
    assert selected_clipped
    assert json.loads(projected)["text"].startswith("Read 1 line(s) from page.txt")
    assert "first-line" not in json.loads(projected)["text"]
    assert services.reads == prior_reads

    def absent(_digest: str, _offset: int, _limit: int) -> ArtifactPage:
        raise FileNotFoundError("missing page")

    with pytest.raises(FileNotFoundError, match="missing page"):
        project_tool_result(result, max_bytes=2048, artifact_reader=absent)


def test_read_raster_streams_original_and_projection_reads_every_page(tmp_path: Path) -> None:
    services = _ArtifactServices(tmp_path / "artifacts")
    png = rgb_png()
    width, height, pixels = decode_rgb_png(png)
    assert (width, height, len(pixels)) == (384, 300, 384 * 300 * 3)
    assert len(png) > MAX_PAGE_BYTES
    (tmp_path / "screen.png").write_bytes(png)
    result = ReadTool().invoke(
        ToolCall("read", {"path": "screen.png"}),
        context=ToolContext(workspace=tmp_path, session_id="s", run_id="r", invocation_id="i", revision_id="v", actor="model"),
        services=services,
    )
    assert isinstance(result, ToolSuccess) and isinstance(result.output, AttachmentOutput)
    assert result.output.data_uri is None and isinstance(result.output.bounds.reference, ArtifactReference)
    result = _canonical(services, result)
    _, images, clipped = project_tool_result(result, max_bytes=2048, artifact_reader=services.page)
    assert not clipped and len(images) == 1
    assert base64.b64decode(images[0].data) == png
    assert decode_rgb_png(base64.b64decode(images[0].data))[:2] == (384, 300)
    assert len(services.reads) >= 2 and all(limit <= MAX_PAGE_BYTES for _, limit in services.reads)


class _ImageMessagesServer(ThreadingHTTPServer):
    def __init__(self, *, deferred: bool = False, image_count: int = 1) -> None:
        super().__init__(("127.0.0.1", 0), _ImageMessagesHandler)
        self.requests: list[dict[str, object]] = []
        self.deferred = deferred
        self.image_count = image_count


class _ImageMessagesHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(_ImageMessagesServer, self.server)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.requests.append(request)
        first = len(server.requests) == 1
        name = PROBE_TOOL if server.deferred else "read"
        arguments = {"action": "invoke_tool", "name": "read", "arguments": {"path": "screen.png"}} if server.deferred else {"path": "screen.png"}
        blocks = (
            [{"type": "tool_use", "id": f"read-screen-{index}", "name": name, "input": arguments} for index in range(server.image_count)]
            if first
            else [{"type": "text", "text": ""}]
        )
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg-image",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            },
        ]
        for index, block in enumerate(blocks):
            events.append({"type": "content_block_start", "index": index, "content_block": block})
            if not first:
                events.append({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": "image received"}})
            events.append({"type": "content_block_stop", "index": index})
        events.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use" if first else "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
                {"type": "message_stop"},
            ]
        )
        body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _messages_server(*, deferred: bool = False, image_count: int = 1) -> Iterator[_ImageMessagesServer]:
    server = _ImageMessagesServer(deferred=deferred, image_count=image_count)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("deferred,image_count", [(False, 1), (True, 1), (False, 10)])
def test_real_core_native_and_untrusted_deferred_images_reach_anthropic_sdk_wire(tmp_path: Path, deferred: bool, image_count: int) -> None:
    with _messages_server(deferred=deferred, image_count=image_count) as server:
        extras = {PLUGIN_ID: {"path": str(write_s5_plugin(tmp_path / "probe"))}} if deferred else None
        workspace = _configure(tmp_path, provider="anthropic", base_url=f"http://127.0.0.1:{server.server_port}", extra_plugins=extras)
        png = rgb_png()
        (workspace / "screen.png").write_bytes(png)
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("Read the screenshot and describe it.")
            assert result.status == "completed", result.error
            assert result.output == "image received" and len(server.requests) == 2
            messages = server.requests[-1]["messages"]
            assert isinstance(messages, list)
            blocks = [block for message in messages for block in message.get("content", [])]
            if deferred:
                images = [block for block in blocks if block.get("type") == "image"]
                assert any("[Untrusted deferred tool result:" in block.get("text", "") for block in blocks)
            else:
                tools = [block for block in blocks if block.get("type") == "tool_result"]
                assert {tool["tool_use_id"] for tool in tools} == {f"read-screen-{index}" for index in range(image_count)}
                images = [block for tool in tools for block in tool["content"] if block.get("type") == "image"]
            assert len(images) == image_count
            for image in images:
                received = base64.b64decode(image["source"]["data"], validate=True)
                assert received == png and decode_rgb_png(received)[:2] == (384, 300)
            events = core.events_after(result.session_id)
            reads = [event for event in events if event.event_type == "core.tool_completed" and event.payload["call"]["tool_name"] == "read"]
            assert len(reads) == image_count
            for event in reads:
                wire = event.payload["result"]
                assert wire["output"]["data_uri"] is None and len(json.dumps(json_wire_object(wire))) < 4096
                # Small-result contract: the durable event is complete, so no canonical
                # spill exists; the image bytes stay recoverable via the output reference.
                assert wire.get("result_reference") is None
                reference = wire["output"]["bounds"]["reference"]
                assert reference is not None and int(reference["bytes"]) == len(png)
                recovered = bytearray()
                offset = 0
                while True:
                    page = core.read_artifact(str(reference["sha256"]), offset=offset, limit=256 * 1024)
                    recovered += page.data
                    if page.eof:
                        break
                    offset += len(page.data)
                assert bytes(recovered) == png
            dispatches = [event for event in events if event.event_type == "core.provider_intent"]
            assert max(len(json.dumps(json_wire_object(event.payload))) for event in dispatches) < 16384
            from voidcode.sdk.transcript import encode_context_history

            history = json.dumps(encode_context_history(core.context_history(result.session_id)), ensure_ascii=False)
            assert base64.b64encode(png).decode("ascii") not in history
            if image_count == 10:
                assert sum(len(image["source"]["data"]) for image in images) > 4 * 1024 * 1024


@pytest.mark.parametrize("name,model", [("anthropic", "claude-sonnet-4-5"), ("google", "gemini-2.5-flash")])
def test_real_native_sdks_send_valid_tool_result_image_blocks(name: str, model: str) -> None:
    server = _JsonLoopback()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    png = rgb_png(160, 160)
    assert len(png) > 50 * 1024
    provider = _provider({name: {"api_key": "native-image-key", "base_url": server.base_url}}, name=name)
    image = ImageAttachment("image/png", base64.b64encode(png).decode("ascii"))
    try:
        result = provider.propose_turn(
            _request(
                ProviderMessage("assistant", tool_calls=(ToolCall("read", {"path": "screen.png"}, "call-image"),)),
                ProviderMessage("tool", "image result", tool_call_id="call-image", tool_name="read", images=(image,)),
                provider_name=name,
                model_name=model,
                raw_model=f"{name}/{model}",
            )
        )
        assert result.output == f"{name}-ok"
        wire = json.loads(server.requests[-1].body)
        if name == "anthropic":
            tool = wire["messages"][-1]["content"][0]
            assert tool["type"] == "tool_result" and tool["tool_use_id"] == "call-image"
            encoded = next(block["source"]["data"] for block in tool["content"] if block["type"] == "image")
        else:
            parts = wire["contents"][-1]["parts"]
            assert parts[0]["functionResponse"]["name"] == "read"
            encoded = next(part["inlineData"]["data"] for part in parts if "inlineData" in part)
        decoded = base64.b64decode(encoded, altchars=b"-_" if name == "google" else None, validate=True)
        assert decoded == png and decode_rgb_png(decoded)[:2] == (160, 160)
    finally:
        provider.close()
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_real_core_openai_tool_image_uses_synthetic_without_silent_drop(tmp_path: Path) -> None:
    with loopback_server([_turn(_call("image", "read", path="screen.png")), _turn(output="image received")]) as server:
        workspace = _configure(tmp_path, provider="endpoint", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        png = rgb_png()
        (workspace / "screen.png").write_bytes(png)
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("Read the image.")
            assert result.status == "completed", result.error
            assert result.output == "image received"
            assert len(server.requests) == 2
            messages = server.requests[1]["messages"]
            assert isinstance(messages, list)
            assert all(message.get("role") != "tool" for message in messages if isinstance(message, dict))
            image_urls: list[str] = []
            for message in messages:
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "image_url":
                            url = block.get("image_url", {}).get("url", "")
                            image_urls.append(url)
            assert len(image_urls) == 1
            prefix, _, data = image_urls[0].partition(",")
            assert prefix == "data:image/png;base64"
            assert base64.b64decode(data) == png


def test_real_core_selected_file_page_preserves_coverage_and_allows_edit(tmp_path: Path) -> None:
    original = "".join(f"line-{index:04d} " + "x" * 90 + "\n" for index in range(256))
    assert 25 * 1024 < len(original.encode()) < 26 * 1024
    old = "line-0128 " + "x" * 90
    new = "line-0128 " + "y" * 90
    digest = hashlib.sha256(original.encode()).hexdigest()
    with loopback_server(
        [
            _turn(_call("selected-read", "read", path="document.txt")),
            _turn(_call("selected-edit", "edit", path="document.txt", oldString=old, newString=new, expectedHash=digest)),
            _turn(output="edited the fully observed file"),
        ]
    ) as server:
        workspace = _configure(tmp_path, provider="endpoint", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        target = workspace / "document.txt"
        target.write_text(original, encoding="utf-8")
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("Read the whole file and change line 128.")
            assert result.status == "completed", result.error
            assert target.read_text() == original.replace(old, new, 1)
            completed = [event for event in core.events_after(result.session_id) if event.event_type == "core.tool_completed"]
            assert len(completed) == 2 and isinstance(decode_tool_result(completed[1].payload["result"]), ToolSuccess)
            observed = next(
                fact
                for fact in core.plugin_runtime.fact_page(result.session_id, 0, 200).facts
                if isinstance(fact, InvocationFinishedFact) and fact.tool_call_id == "selected-read"
            )
            assert not observed.truncated and not observed.partial
            assert len(observed.reads) == 1 and observed.reads[0].whole_file
            assert not observed.reads[0].clipped and observed.reads[0].lines == tuple(range(1, 257))
            projected = json.loads(next(message["content"] for message in server.requests[1]["messages"] if message.get("role") == "tool"))
            assert projected["text"].count(original) == 1 and "body" not in projected
            assert "text_partial" not in projected
            reference = decode_tool_result(completed[0].payload["result"]).result_reference
            assert reference is not None and projected["result_uri"] == reference.uri
            complete = json.loads(read_artifact_content(reference, core.artifact_page))
            assert complete["body"]["raw_content"] == original and len(complete["body"]["lines"]) == 256
        with open_core(workspace, state_dir=tmp_path / "state") as reopened:
            assert json.loads(read_artifact_content(reference, reopened.artifact_page))["body"]["raw_content"] == original
            assert len(server.requests) == 3 and target.read_text() == original.replace(old, new, 1)


def test_real_core_projects_application_json_web_fetch_content(tmp_path: Path) -> None:
    content = b'{"answer": "actual JSON fetch content", "count": 7}'

    class JsonHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            pass

    web = ThreadingHTTPServer(("127.0.0.1", 0), JsonHandler)
    web_worker = threading.Thread(target=web.serve_forever, daemon=True)
    web_worker.start()
    url = f"http://127.0.0.1:{web.server_port}/result.json"
    try:
        with loopback_server(
            [
                _turn(_call("json-page", "web_fetch", url=url)),
                _turn(output="JSON response received"),
            ]
        ) as provider:
            workspace = _configure(
                tmp_path,
                provider="endpoint",
                base_url=f"http://127.0.0.1:{provider.server_port}/v1",
                extra_plugins={"web": {"enabled": True}},
            )
            with open_core(workspace, state_dir=tmp_path / "state") as core:
                result = core.run("Fetch the JSON page and use its content.")
                assert result.status == "completed", result.error
                message = next(item["content"] for item in provider.requests[-1]["messages"] if item.get("role") == "tool")
                projection = json.loads(message)
                assert projection["text"] == content.decode("utf-8")
                assert projection["body"]["content_type"] == "application/json"
                assert projection["body"]["artifact"]["sha256"] == projection["output_uri"].removeprefix("artifact://")
                assert "text_partial" not in projection
                assert len(provider.requests) == 2
    finally:
        web.shutdown()
        web_worker.join(timeout=5)
        web.server_close()


def test_real_shell_capture_larger_than_wire_frame_is_retrievable_but_not_duplicated_in_event(tmp_path: Path) -> None:
    pattern = "captured π text\\t\\n\n"
    repeats = 300000
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(f'import sys; sys.stdout.write({pattern!r} * {repeats})')}"
    with loopback_server([_turn(_call("capture", "shell_exec", command=command)), _turn(output="capture received")]) as server:
        workspace = _configure(tmp_path, provider="endpoint", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("Capture the command output.")
            assert result.status == "completed", result.error
            event = next(event for event in core.store.events(result.session_id) if event.event_type == "core.tool_completed")
            wire = event.payload["result"]
            assert len(json.dumps(json_wire_object(wire)).encode()) < 4096
            reference = ArtifactReference(**wire["body"]["stdout"])
            assert reference.bytes > 4 * 1024 * 1024
            captured = read_artifact_content(reference, core.artifact_page).decode()
            assert captured == pattern * repeats
            projection = next(message["content"] for message in server.requests[-1]["messages"] if message.get("role") == "tool")
            assert len(projection.encode()) <= TOOL_OUTPUT_BYTES
            document = json.loads(projection)
            assert document["text"].startswith(pattern) and document["text_partial"]
            assert document["output_uri"] == reference.uri
            assert document["body"]["stdout"] == reference.as_payload()


def test_tool_result_codec_and_diagnostics_retain_original_data() -> None:
    reference = ArtifactReference("c" * 64, 1000, "application/json")
    diagnostics = ToolDiagnostics(summary="s" * 8000, guidance="g" * 8000, details={"long": "d" * 8000})
    result = ToolFailure("inspect", "error", diagnostics=diagnostics, result_reference=reference)
    assert decode_tool_result(encode_tool_result(result)) == result
    assert diagnostics.summary == "s" * 8000 and diagnostics.details["long"] == "d" * 8000
    with pytest.raises(ValueError, match="finite"):
        ToolDiagnostics(details={"bad": float("nan")})


@pytest.mark.parametrize("failure", ["chunk", "producer", "revoked", "cancelled", "bound"])
def test_streaming_artifact_ingress_discards_incomplete_spills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    workspace = _configure(tmp_path, provider="endpoint", base_url="http://127.0.0.1:1/v1")
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        session = core.create_session()
        lease = core.plugin_runtime._open_lease(
            owner="files", session_id=session.session_id, run_id="r", invocation_id="i", readonly=True, actor="model"
        )
        services = RuntimeServices(core.plugin_runtime, lease)
        if failure == "bound":
            monkeypatch.setattr("voidcode.core.artifacts.MAX_ARTIFACT_BYTES", 4)

        def chunks():
            yield b"abc"
            if failure == "producer":
                raise RuntimeError("producer failed")
            if failure == "revoked":
                lease.revoked = True
            if failure == "cancelled":
                core.plugin_runtime.cancel_event_for(lease).set()
            yield "not bytes" if failure == "chunk" else b"def"

        expected = {
            "chunk": TypeError,
            "producer": RuntimeError,
            "revoked": PermissionError,
            "cancelled": CancelledError,
            "bound": ValueError,
        }[failure]
        with pytest.raises(expected):
            services.spill_artifact_stream(chunks())
        assert list((tmp_path / "state" / "artifacts").iterdir()) == []


def test_real_core_persists_canonical_original_before_compacting_large_result(tmp_path: Path) -> None:
    source = tmp_path / "result_plugin"
    source.mkdir()
    (source / "__init__.py").write_text(
        "from voidcode.sdk.plugin import Plugin\n"
        "from voidcode.sdk.services import ArtifactReference\n"
        "from voidcode.sdk.tools import ToolDefinition, ToolFailure, TextOutput, OpaqueToolBody, ToolDiagnostics\n"
        "class Result:\n"
        "    definition = ToolDefinition('large_result', 'Return a large structured failure')\n"
        "    def invoke(self, call, *, context, services):\n"
        "        return ToolFailure(call.tool_name, '原因' * 20000 + 'LATE_ROOT_CAUSE', TextOutput('recoverable preview'),\n"
        "            OpaqueToolBody({'original': 'x' * 500000, 'tail': 'ORIGINAL_BODY_END'}),\n"
        "            diagnostics=ToolDiagnostics(summary='diagnostic' * 1000),\n"
        "            result_reference=ArtifactReference('0' * 64, 0, 'application/json'))\n"
        "def create_plugin(config):\n"
        "    return Plugin(tools=(Result(),))\n",
        encoding="utf-8",
    )
    with loopback_server([_turn(_call("large", "large_result")), _turn(output="recovery identity received")]) as server:
        workspace = _configure(
            tmp_path,
            provider="endpoint",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            extra_plugins={"result_fixture": {"path": str(source)}},
        )
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("Read the structured result, keeping its recovery identity.")
            assert result.status == "completed", result.error
            event = next(event for event in core.events_after(result.session_id) if event.event_type == "core.tool_completed")
            compact = decode_tool_result(event.payload["result"])
            assert isinstance(compact, ToolFailure) and compact.body is None
            assert isinstance(compact.output, TextOutput) and compact.output.text == "recoverable preview"
            assert not compact.output.bounds.partial
            reference = compact.result_reference
            assert reference is not None and reference.sha256 != "0" * 64
            original_bytes = read_artifact_content(reference, core.artifact_page)
            original = json.loads(original_bytes)
            assert "result_reference" not in original
            assert original["body"]["tail"] == "ORIGINAL_BODY_END" and len(original["body"]["original"]) == 500000
            assert original["error"].endswith("LATE_ROOT_CAUSE")
            assert original["diagnostics"]["summary"] == "diagnostic" * 1000
            assert len(json.dumps(encode_tool_result(compact)).encode()) < 256 * 1024
            provider_text = next(message["content"] for message in server.requests[-1]["messages"] if message.get("role") == "tool")
            assert reference.uri in provider_text and len(provider_text.encode()) <= TOOL_OUTPUT_BYTES
        with open_core(workspace, state_dir=tmp_path / "state") as reopened:
            assert read_artifact_content(reference, reopened.artifact_page) == original_bytes
            assert len(server.requests) == 2


def test_missing_image_page_fails_durably_without_dispatching_a_text_only_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _messages_server() as server:
        workspace = _configure(tmp_path, provider="anthropic", base_url=f"http://127.0.0.1:{server.server_port}")
        (workspace / "screen.png").write_bytes(rgb_png())
        with open_core(workspace, state_dir=tmp_path / "state") as core:

            def missing(digest: str) -> Path:
                raise ArtifactNotFoundError(f"missing image artifact: {digest}")

            monkeypatch.setattr(core._artifacts, "resolve", missing)
            result = core.run("Read the screenshot.")
            assert result.status == "failed" and "artifact is not stored" in result.error
            assert len(server.requests) == 1
            assert any(event.event_type == "core.tool_completed" for event in core.store.events(result.session_id))
            assert any(event.event_type == "core.run_failed" for event in core.store.events(result.session_id))
