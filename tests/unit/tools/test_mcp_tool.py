from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import sys
import wave
import zlib
from pathlib import Path

import pytest

import voidcode.tools.mcp as mcp_module
from voidcode.core.tool_context import ArtifactBinary, ArtifactPage, ToolContext
from voidcode.core.transcript import ToolResultView, project_report
from voidcode.core.turns import FinalTurn, ToolTurn
from voidcode.mcp.types import McpToolCallResult, McpToolDescriptor
from voidcode.runtime.config.loading import RuntimeConfig, RuntimeMcpConfig, RuntimeMcpServerConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.fact_codec import reported_call_from_event
from voidcode.runtime.permissions.policy import PermissionPolicy
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.security.redaction import TOOL_OUTPUT_BYTES
from voidcode.tools.contracts import AttachmentOutput, TextOutput, ToolCall, ToolFailure, ToolSuccess
from voidcode.tools.mcp import McpTool


def _png(color: tuple[int, int, int, int]) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00" + bytes(color))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def _wav() -> bytes:
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00")
    return data.getvalue()


def _invoke(result: McpToolCallResult, workspace: Path, *, call_id: str = "mcp-call") -> ToolSuccess | ToolFailure:
    descriptor = McpToolDescriptor(
        server_name="media",
        tool_name="inspect",
        description="Test MCP result projection",
        input_schema={"type": "object"},
    )
    tool = McpTool(descriptor)
    return tool.invoke(
        ToolCall(tool_name=tool.definition.name, tool_call_id=call_id, arguments={}),
        context=ToolContext(
            workspace=workspace,
            session_id="mcp-test-session",
            invocation_id=call_id,
            mcp_request=lambda **_: result,
        ),
    )


def _body(result: ToolSuccess | ToolFailure) -> dict[str, object]:
    assert result.body is not None
    return result.body.as_payload()


def _artifacts(body: dict[str, object]) -> list[dict[str, object]]:
    artifacts = body.get("artifacts")
    assert isinstance(artifacts, list)
    assert all(isinstance(artifact, dict) for artifact in artifacts)
    return [artifact for artifact in artifacts if isinstance(artifact, dict)]


def _result_json(body: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    result_artifact = body["result_artifact"]
    assert isinstance(result_artifact, dict)
    raw = Path(str(result_artifact["path"])).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result_artifact["sha256"]
    decoded = json.loads(raw)
    assert isinstance(decoded, dict)
    return result_artifact, decoded


def _isolate_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("VOIDCODE_DB_PATH", raising=False)


def test_mcp_full_result_json_and_single_image_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_artifacts(monkeypatch, tmp_path)
    png = _png((5, 25, 125, 255))
    content = [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode("ascii")}]
    structured = {"nested": {"items": [1, True, "雪"]}}
    result = _invoke(McpToolCallResult(content=content, structured_content=structured), tmp_path)

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.output, AttachmentOutput)
    body = _body(result)
    json_artifact, original = _result_json(body)
    assert json_artifact["mime"] == "application/json"
    assert original == {"content": content, "structured_content": structured, "is_error": False}
    assert body["content"] == content
    assert body["structured_content"] == structured
    assert body["is_error"] is False
    artifacts = body["artifacts"]
    assert isinstance(artifacts, list) and len(artifacts) == 2
    image_artifact = next(artifact for artifact in artifacts if artifact["kind"] == "mcp-image")
    assert isinstance(image_artifact, dict)
    assert Path(str(image_artifact["path"])).read_bytes() == png
    assert image_artifact["sha256"] == hashlib.sha256(png).hexdigest()
    assert result.output.bounds.reference is not None
    assert result.output.bounds.reference.uri == image_artifact["uri"]
    assert result.output.data_uri is None
    assert result.output.presentation is not None
    assert f"Full MCP result JSON: {json_artifact['uri']}" in result.output.presentation


def test_mcp_media_resources_and_links_keep_raw_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_artifacts(monkeypatch, tmp_path)
    png_one = _png((20, 80, 140, 255))
    png_two = _png((200, 90, 30, 255))
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" />'
    audio = _wav()
    pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
    content = [
        {"type": "text", "text": "visible MCP text"},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png_one).decode("ascii")},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png_two).decode("ascii")},
        {"type": "image", "mimeType": "image/svg+xml", "data": base64.b64encode(svg).decode("ascii")},
        {"type": "audio", "mimeType": "audio/wav", "data": base64.b64encode(audio).decode("ascii")},
        {
            "type": "resource",
            "resource": {"uri": "voidcode://resource/notes", "mimeType": "text/plain", "text": "embedded resource text"},
        },
        {
            "type": "resource",
            "resource": {"uri": "voidcode://resource/document", "mimeType": "application/pdf", "blob": base64.b64encode(pdf).decode("ascii")},
        },
        {
            "type": "resource_link",
            "uri": "https://example.invalid/remote",
            "name": "remote-doc",
            "title": "Remote title",
            "description": "ResourceLink text",
            "mimeType": 'application/pdf; title="a;b"',
        },
    ]
    result = _invoke(McpToolCallResult(content=content, structured_content={"ok": True}), tmp_path)

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.output, TextOutput)
    assert "visible MCP text" in result.output.text
    assert "embedded resource text" in result.output.text
    assert "https://example.invalid/remote" in result.output.text
    assert "ResourceLink text" in result.output.text
    assert "remote resources are not fetched" in result.output.text
    body = _body(result)
    json_artifact, original = _result_json(body)
    assert original["content"] == content
    assert f"Full MCP result JSON: {json_artifact['uri']}" in result.output.text
    assert result.output.bounds.reference is not None
    assert result.output.bounds.reference.uri == json_artifact["uri"]
    artifacts = body["artifacts"]
    assert isinstance(artifacts, list) and len(artifacts) == 6
    json_reference = next(artifact for artifact in artifacts if artifact["kind"] == "mcp-result")
    assert json_reference["uri"] == json_artifact["uri"]
    media_artifacts = [artifact for artifact in artifacts if artifact["kind"] != "mcp-result"]
    assert len(media_artifacts) == 5
    expected = {
        "mcp-image": {hashlib.sha256(png_one).hexdigest(), hashlib.sha256(png_two).hexdigest()},
        "mcp-svg": {hashlib.sha256(svg).hexdigest()},
        "mcp-audio": {hashlib.sha256(audio).hexdigest()},
        "mcp-resource": {hashlib.sha256(pdf).hexdigest()},
    }
    seen: dict[str, set[str]] = {kind: set() for kind in expected}
    for artifact in media_artifacts:
        assert isinstance(artifact, dict)
        raw = Path(str(artifact["path"])).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert digest == artifact["sha256"]
        assert len(raw) == artifact["byte_count"]
        kind = str(artifact["kind"])
        assert digest in expected[kind]
        seen[kind].add(digest)
    assert seen == expected
    svg_artifact = next(artifact for artifact in media_artifacts if artifact["kind"] == "mcp-svg")
    assert svg_artifact["mime"] == "image/svg+xml"
    assert Path(str(svg_artifact["path"])).read_bytes() == svg
    pdf_artifact = next(artifact for artifact in media_artifacts if artifact["kind"] == "mcp-resource")
    assert pdf_artifact["mime"] == "application/pdf"


def test_mcp_server_error_keeps_partial_image_and_original_result_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_artifacts(monkeypatch, tmp_path)
    png = _png((10, 40, 90, 255))
    content = [
        {"type": "text", "text": "partial message"},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode("ascii")},
    ]
    result = _invoke(McpToolCallResult(content=content, structured_content={"status": "partial"}, is_error=True), tmp_path)

    assert isinstance(result, ToolFailure)
    assert isinstance(result.output, AttachmentOutput)
    assert result.output.bounds.partial
    body = _body(result)
    json_artifact, original = _result_json(body)
    assert original == {"content": content, "structured_content": {"status": "partial"}, "is_error": True}
    assert body["is_error"] is True
    assert "projection_error" not in body
    artifacts = body["artifacts"]
    assert isinstance(artifacts, list) and len(artifacts) == 2
    image_artifact = next(artifact for artifact in artifacts if artifact["kind"] == "mcp-image")
    assert isinstance(image_artifact, dict)
    assert result.output.bounds.reference is not None
    assert result.output.bounds.reference.uri == image_artifact["uri"]
    assert result.output.presentation is not None


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"type": "image", "mimeType": "image/png", "data": "%%%"}, "base64"),
        (
            {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(_png((1, 2, 3, 255))).decode("ascii")},
            "does not match",
        ),
        ({"type": "image", "mimeType": "image/png;invalid", "data": base64.b64encode(_png((1, 2, 3, 255))).decode("ascii")}, "MIME type"),
        (
            {
                "type": "image",
                "mimeType": "image/png;=invalid",
                "data": base64.b64encode(_png((1, 2, 3, 255))).decode("ascii"),
            },
            "MIME type",
        ),
        (
            {
                "type": "image",
                "mimeType": "image/png; charset=",
                "data": base64.b64encode(_png((1, 2, 3, 255))).decode("ascii"),
            },
            "MIME type",
        ),
        (
            {
                "type": "image",
                "mimeType": 'image/png; title="unclosed',
                "data": base64.b64encode(_png((1, 2, 3, 255))).decode("ascii"),
            },
            "MIME type",
        ),
        (
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"x" * (len(_png((1, 2, 3, 255))) + 1)).decode("ascii")},
            "exceeds",
        ),
        ({"type": "vendor-image", "mimeType": "image/png", "data": "AA=="}, "unsupported MCP content type"),
    ],
)
def test_mcp_invalid_content_fails_with_complete_json_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    item: dict[str, object],
    message: str,
) -> None:
    _isolate_artifacts(monkeypatch, tmp_path)
    assert mcp_module._MAX_MCP_BINARY_BYTES == 50 * 1024 * 1024
    monkeypatch.setattr(mcp_module, "_MAX_MCP_BINARY_BYTES", len(_png((1, 2, 3, 255))))
    content = [{"type": "text", "text": "before invalid item"}, item]
    result = _invoke(McpToolCallResult(content=content, structured_content={"kept": True}), tmp_path)

    assert isinstance(result, ToolFailure)
    assert not isinstance(result.output, AttachmentOutput)
    body = _body(result)
    assert message in str(body["projection_error"])
    result_artifact, original = _result_json(body)
    assert original == {"content": content, "structured_content": {"kept": True}, "is_error": False}
    assert "artifacts" not in body
    output = result.output.text if isinstance(result.output, TextOutput) else ""
    assert f"Full MCP result JSON: {result_artifact['uri']}" in output
    assert "before invalid item" in output


def _stdio_media_server_source(png_one: bytes, png_two: bytes, svg: bytes, audio: bytes, pdf: bytes) -> str:
    png_one_base64 = base64.b64encode(png_one).decode("ascii")
    png_two_base64 = base64.b64encode(png_two).decode("ascii")
    svg_base64 = base64.b64encode(svg).decode("ascii")
    audio_base64 = base64.b64encode(audio).decode("ascii")
    pdf_base64 = base64.b64encode(pdf).decode("ascii")
    return (
        "import asyncio\n"
        "from mcp.server.lowlevel import Server\n"
        "from mcp.server.stdio import stdio_server\n"
        "from mcp.types import AudioContent, BlobResourceContents, CallToolResult, "
        "EmbeddedResource, ImageContent, ListToolsResult, ResourceLink, TextContent, "
        "TextResourceContents, Tool\n"
        f"PNG_ONE = {png_one_base64!r}\n"
        f"PNG_TWO = {png_two_base64!r}\n"
        f"SVG = {svg_base64!r}\n"
        f"AUDIO = {audio_base64!r}\n"
        f"PDF = {pdf_base64!r}\n"
        "async def on_list_tools(context, params):\n"
        "    return ListToolsResult(tools=[\n"
        "        Tool(name='media_bundle', inputSchema={'type': 'object', 'properties': {}}),\n"
        "        Tool(name='single_png', inputSchema={'type': 'object', 'properties': {}}),\n"
        "        Tool(name='partial_error', inputSchema={'type': 'object', 'properties': {}}),\n"
        "    ])\n"
        "async def on_call_tool(context, params):\n"
        "    if params.name == 'media_bundle':\n"
        "        return CallToolResult(content=[\n"
        "            TextContent(text='stdio SDK media bundle text'),\n"
        "            ImageContent(data=PNG_ONE, mimeType='image/png'),\n"
        "            ImageContent(data=PNG_TWO, mimeType='image/png'),\n"
        "            ImageContent(data=SVG, mimeType='image/svg+xml'),\n"
        "            AudioContent(data=AUDIO, mimeType='audio/wav'),\n"
        "            EmbeddedResource(resource=TextResourceContents(uri='voidcode://resource/notes', "
        "mimeType='text/plain', text='embedded text from stdio')),\n"
        "            EmbeddedResource(resource=BlobResourceContents(uri='voidcode://resource/document', "
        "mimeType='application/pdf', blob=PDF)),\n"
        "            ResourceLink(name='remote-doc', title='Remote title', "
        "uri='https://example.invalid/remote', description='ResourceLink visible text', "
        "mimeType='application/pdf'),\n"
        "            TextContent(text='large bundle tail ' + ('b' * 60000)),\n"
        "        ], structuredContent={'bundle': 'complete'})\n"
        "    if params.name == 'single_png':\n"
        "        caption = 'single PNG caption ' + ('c' * 60000)\n"
        "        return CallToolResult(content=[TextContent(text=caption), "
        "ImageContent(data=PNG_ONE, mimeType='image/png')], "
        "structuredContent={'large': 's' * 60000})\n"
        "    if params.name == 'partial_error':\n"
        "        return CallToolResult(content=[TextContent(text='partial response'), "
        "ImageContent(data=PNG_TWO, mimeType='image/png')], "
        "structuredContent={'partial': True}, isError=True)\n"
        "    return CallToolResult(content=[TextContent(text='unknown tool')], isError=True)\n"
        "server = Server('native-mcp-media', on_list_tools=on_list_tools, on_call_tool=on_call_tool)\n"
        "async def main():\n"
        "    async with stdio_server() as (read_stream, write_stream):\n"
        "        await server.run(read_stream, write_stream, server.create_initialization_options())\n"
        "asyncio.run(main())\n"
    )


class _McpMediaGraph:
    def __init__(self) -> None:
        self._next_call = 0
        self.final_tool_results: tuple[object, ...] = ()

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> FinalTurn | ToolTurn:
        _ = request, session
        if self._next_call == 0:
            name, call_id = "mcp/media/media_bundle", "media-bundle"
        elif self._next_call == 1:
            name, call_id = "mcp/media/single_png", "single-png"
        elif self._next_call == 2:
            name, call_id = "mcp/media/partial_error", "partial-error"
        else:
            self.final_tool_results = tool_results
            return FinalTurn(output="MCP native media proof complete")
        self._next_call += 1
        return ToolTurn(calls=(ToolCall(tool_name=name, tool_call_id=call_id, arguments={}),))


def test_stdio_mcp_media_and_large_results_through_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_artifacts(monkeypatch, tmp_path)
    png_one = _png((20, 80, 140, 255))
    png_two = _png((200, 90, 30, 255))
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" />'
    audio = _wav()
    pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
    server = tmp_path / "media_server.py"
    server.write_text(_stdio_media_server_source(png_one, png_two, svg, audio, pdf), encoding="utf-8")
    session_id = "mcp-native-media-engine"
    graph = _McpMediaGraph()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=graph,
        config=RuntimeConfig(
            execution_engine="deterministic",
            mcp=RuntimeMcpConfig(
                enabled=True,
                servers={"media": RuntimeMcpServerConfig(command=(sys.executable, str(server)))},
                request_timeout_seconds=10,
            ),
        ),
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    try:
        chunks = list(runtime.run_stream(RuntimeRequest(prompt="inspect MCP media", session_id=session_id)))
        events = [
            chunk.event
            for chunk in chunks
            if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
        ]
        reports = [report for event in events if (report := reported_call_from_event(event)) is not None]
        assert {report.final_tool_name for report in reports} == {
            "mcp/media/media_bundle",
            "mcp/media/single_png",
            "mcp/media/partial_error",
        }

        bundle_report = next(report for report in reports if report.final_tool_name == "mcp/media/media_bundle")
        bundle_view = project_report(bundle_report)
        assert isinstance(bundle_view.output, TextOutput)
        bundle_body = _body(bundle_report.result)
        bundle_json_artifact, bundle_original = _result_json(bundle_body)
        assert bundle_original["is_error"] is False
        assert bundle_view.output.bounds.truncated
        assert len(bundle_view.output.text.encode("utf-8")) <= TOOL_OUTPUT_BYTES
        assert bundle_view.output.bounds.reference is not None
        assert bundle_view.output.bounds.reference.uri == bundle_json_artifact["uri"]
        bundle_content = bundle_original["content"]
        assert isinstance(bundle_content, list) and bundle_content
        large_text_item = bundle_content[-1]
        assert isinstance(large_text_item, dict)
        large_text = large_text_item.get("text")
        assert isinstance(large_text, str) and len(large_text) > TOOL_OUTPUT_BYTES
        assert "stdio SDK media bundle text" in bundle_view.output.text
        assert "embedded text from stdio" in bundle_view.output.text
        assert "ResourceLink visible text" in bundle_view.output.text
        assert "https://example.invalid/remote" in bundle_view.output.text
        assert "remote resources are not fetched" in bundle_view.output.text
        assert f"Full MCP result JSON: {bundle_json_artifact['uri']}" in bundle_view.output.text
        bundle_artifacts = bundle_body["artifacts"]
        assert isinstance(bundle_artifacts, list) and len(bundle_artifacts) == 6
        bundle_json_reference = next(artifact for artifact in bundle_artifacts if artifact["kind"] == "mcp-result")
        assert bundle_json_reference["uri"] == bundle_json_artifact["uri"]
        media_artifacts = [artifact for artifact in bundle_artifacts if artifact["kind"] != "mcp-result"]
        expected_hashes = {
            "mcp-image": {hashlib.sha256(png_one).hexdigest(), hashlib.sha256(png_two).hexdigest()},
            "mcp-svg": {hashlib.sha256(svg).hexdigest()},
            "mcp-audio": {hashlib.sha256(audio).hexdigest()},
            "mcp-resource": {hashlib.sha256(pdf).hexdigest()},
        }
        actual_hashes = {kind: set() for kind in expected_hashes}
        for artifact in media_artifacts:
            assert isinstance(artifact, dict)
            artifact_id = artifact["artifact_id"]
            tool_call_id = artifact["tool_call_id"]
            assert isinstance(artifact_id, str) and isinstance(tool_call_id, str)
            if artifact["kind"] == "mcp-svg":
                read_result = runtime.read_tool_output_artifact(
                    caller_session_id=session_id,
                    session_id=session_id,
                    artifact_id=artifact_id,
                    tool_call_id=tool_call_id,
                    byte_offset=0,
                    byte_limit=4096,
                )
                assert isinstance(read_result, ArtifactPage)
                raw = read_result.text.encode("utf-8")
                assert read_result.sha256 == artifact["sha256"]
            else:
                read_result = runtime.read_tool_output_artifact(
                    caller_session_id=session_id,
                    session_id=session_id,
                    artifact_id=artifact_id,
                    tool_call_id=tool_call_id,
                )
                assert isinstance(read_result, ArtifactBinary) and read_result.data is not None
                raw = read_result.data
                assert read_result.mime == artifact["mime"]
            digest = hashlib.sha256(raw).hexdigest()
            assert digest == artifact["sha256"]
            assert len(raw) == artifact["byte_count"]
            assert digest in expected_hashes[str(artifact["kind"])]
            actual_hashes[str(artifact["kind"])].add(digest)
            assert artifact["uri"] in bundle_view.output.text
        assert actual_hashes == expected_hashes
        bundle_json_artifact_id = bundle_json_artifact["artifact_id"]
        bundle_json_tool_call_id = bundle_json_artifact["tool_call_id"]
        assert isinstance(bundle_json_artifact_id, str) and isinstance(bundle_json_tool_call_id, str)
        bundle_json_read = runtime.read_tool_output_artifact(
            caller_session_id=session_id,
            session_id=session_id,
            artifact_id=bundle_json_artifact_id,
            tool_call_id=bundle_json_tool_call_id,
            limit=0,
        )
        assert isinstance(bundle_json_read, ArtifactBinary)
        assert bundle_json_read.sha256 == bundle_json_artifact["sha256"]

        single_report = next(report for report in reports if report.final_tool_name == "mcp/media/single_png")
        single_view = project_report(single_report)
        assert isinstance(single_view.output, AttachmentOutput)
        assert single_view.output.mime == "image/png"
        assert single_view.output.data_uri is None
        assert single_view.output.presentation is not None
        assert single_view.output.bounds.truncated
        assert len(single_view.output.presentation.encode("utf-8")) <= TOOL_OUTPUT_BYTES
        single_body = _body(single_report.result)
        single_json_artifact, single_original = _result_json(single_body)
        original_content = single_original["content"]
        assert isinstance(original_content, list) and original_content
        caption_item = original_content[0]
        assert isinstance(caption_item, dict)
        caption = caption_item.get("text")
        assert isinstance(caption, str) and len(caption) > TOOL_OUTPUT_BYTES
        original_structured = single_original["structured_content"]
        assert isinstance(original_structured, dict)
        large_value = original_structured.get("large")
        assert isinstance(large_value, str) and len(large_value) > TOOL_OUTPUT_BYTES
        assert f"Full MCP result JSON: {single_json_artifact['uri']}" in single_view.output.presentation
        image_artifact = next(artifact for artifact in _artifacts(single_body) if artifact["kind"] == "mcp-image")
        hydrated_single = next(
            result for result in graph.final_tool_results if isinstance(result, ToolResultView) and result.tool_name == "mcp/media/single_png"
        )
        assert isinstance(hydrated_single.output, AttachmentOutput)
        assert hydrated_single.output.data_uri is not None
        assert single_view.output.bounds.reference is not None
        assert single_view.output.bounds.reference.uri == image_artifact["uri"]
        raw_data_uri = base64.b64decode(hydrated_single.output.data_uri.split(",", 1)[1], validate=True)
        assert raw_data_uri == png_one
        assert hashlib.sha256(raw_data_uri).hexdigest() == image_artifact["sha256"]
        image_artifact_id = image_artifact["artifact_id"]
        image_tool_call_id = image_artifact["tool_call_id"]
        assert isinstance(image_artifact_id, str) and isinstance(image_tool_call_id, str)
        image_read = runtime.read_tool_output_artifact(
            caller_session_id=session_id,
            session_id=session_id,
            artifact_id=image_artifact_id,
            tool_call_id=image_tool_call_id,
        )
        assert isinstance(image_read, ArtifactBinary) and image_read.data == raw_data_uri
        assert image_read.sha256 == image_artifact["sha256"]
        single_json_artifact_id = single_json_artifact["artifact_id"]
        single_json_tool_call_id = single_json_artifact["tool_call_id"]
        assert isinstance(single_json_artifact_id, str) and isinstance(single_json_tool_call_id, str)
        single_json_read = runtime.read_tool_output_artifact(
            caller_session_id=session_id,
            session_id=session_id,
            artifact_id=single_json_artifact_id,
            tool_call_id=single_json_tool_call_id,
            limit=0,
        )
        assert isinstance(single_json_read, ArtifactBinary)
        assert single_json_read.sha256 == single_json_artifact["sha256"]
        partial_report = next(report for report in reports if report.final_tool_name == "mcp/media/partial_error")
        partial_view = project_report(partial_report)
        assert isinstance(partial_view.output, AttachmentOutput)
        assert partial_view.output.bounds.partial
        partial_body = _body(partial_report.result)
        partial_json_artifact, partial_original = _result_json(partial_body)
        assert partial_original["is_error"] is True
        assert partial_view.output.presentation is not None
        assert f"Full MCP result JSON: {partial_json_artifact['uri']}" in partial_view.output.presentation
        partial_image = next(artifact for artifact in _artifacts(partial_body) if artifact["kind"] == "mcp-image")
        assert partial_view.output.bounds.reference is not None
        assert partial_view.output.bounds.reference.uri == partial_image["uri"]
        hydrated_partial = next(
            result for result in graph.final_tool_results if isinstance(result, ToolResultView) and result.tool_name == "mcp/media/partial_error"
        )
        assert isinstance(hydrated_partial.output, AttachmentOutput)
        assert hydrated_partial.output.data_uri is not None
        partial_png = base64.b64decode(hydrated_partial.output.data_uri.split(",", 1)[1], validate=True)
        assert partial_png == png_two
    finally:
        runtime.__exit__(None, None, None)
