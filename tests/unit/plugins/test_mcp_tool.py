"""MCP content projection preserves full artifacts and honest failure bounds."""

import base64
import json
from hashlib import sha256
from pathlib import Path

import pytest

from voidcode.plugins.mcp.mcp_tool import McpTool
from voidcode.plugins.mcp.types import McpCallError, McpToolCallResult
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import (
    CompositeOutput,
    ConfirmedStop,
    JsonPart,
    MediaPart,
    TextPart,
    ToolCall,
    UnconfirmedStop,
    composite_ordered_text,
)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=")


class Manager:
    def __init__(self, result):
        self.result = result

    def call_tool(self, **kwargs):
        return self.result

    def descriptor_for(self, **kwargs):
        return None


class FailingManager:
    def __init__(self, error):
        self.error = error

    def call_tool(self, **kwargs):
        _ = kwargs
        raise self.error

    def descriptor_for(self, **kwargs):
        _ = kwargs
        return None


class RecordingManager:
    def __init__(self):
        self.abort_signal = None
        self.result = McpToolCallResult(content=[])

    def call_tool(self, **kwargs):
        self.abort_signal = kwargs.get("abort_signal")
        return self.result

    def descriptor_for(self, **kwargs):
        _ = kwargs
        return None


class Services:
    def __init__(self):
        self.artifacts = {}

    def spill_artifact(self, content, *, media_type):
        data = content.encode() if isinstance(content, str) else content
        digest = sha256(data).hexdigest()
        self.artifacts[digest] = data
        return ArtifactReference(sha256=digest, bytes=len(data), media_type=media_type)


def invoke(tmp_path, content, *, is_error=False):
    services = Services()
    result = McpTool(manager=Manager(McpToolCallResult(content=content, is_error=is_error))).invoke(
        ToolCall("mcp", {"server": "demo", "tool": "example"}),
        context=ToolContext(workspace=tmp_path, session_id="session", run_id="run", invocation_id="inv", revision_id="rev", actor="model"),
        services=services,
    )
    return result, services


def invoke_manager(tmp_path, manager, *, abort_signal=None):
    services = Services()
    result = McpTool(manager=manager).invoke(
        ToolCall("mcp", {"server": "demo", "tool": "example"}),
        context=ToolContext(
            workspace=tmp_path,
            session_id="session",
            run_id="run",
            invocation_id="inv",
            revision_id="rev",
            actor="model",
            abort_signal=abort_signal,
        ),
        services=services,
    )
    return result, services


def test_empty_content_still_references_the_original_json(tmp_path: Path):
    result, services = invoke(tmp_path, [])
    assert result.status == "ok"
    assert isinstance(result.output, CompositeOutput)
    assert len(result.output.parts) == 1
    first = result.output.parts[0]
    assert isinstance(first, TextPart)
    reference = result.output.bounds.reference
    assert reference is not None
    assert json.loads(services.artifacts[reference.sha256]) == {"content": [], "structured_content": None, "is_error": False}
    assert reference.uri in first.text


@pytest.mark.parametrize("size", [0, 60_000])
def test_image_projection_preserves_binary_and_json_references(tmp_path: Path, size: int):
    data = PNG + b"x" * size
    result, services = invoke(tmp_path, [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(data).decode()}])
    assert result.status == "ok"
    assert isinstance(result.output, CompositeOutput)
    assert len(result.output.parts) == 2
    assert isinstance(result.output.parts[0], TextPart)
    media = result.output.parts[1]
    assert isinstance(media, MediaPart)
    assert media.mime == "image/png"
    assert services.artifacts[sha256(data).hexdigest()] == data
    assert services.artifacts[media.reference.sha256] == data
    assert result.output.bounds.reference is not None
    assert result.output.bounds.reference.media_type == "application/json"
    assert result.output.bounds.truncated is False


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "image", "mimeType": "image/png", "data": "bad base64"}],
        [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"not png").decode()}],
        [{"type": "audio", "mimeType": "text/plain", "data": "YQ=="}],
        [{"type": "unknown"}],
    ],
)
def test_projection_failure_keeps_original_json_available(tmp_path: Path, content):
    result, services = invoke(tmp_path, content)
    assert result.status == "error"
    assert result.output.bounds.partial is True
    reference = result.body.as_payload()["result_artifact"]
    assert json.loads(services.artifacts[reference["sha256"]])["content"] == content
    assert "projection failed" in result.error
    assert isinstance(result.execution, UnconfirmedStop)
    assert result.execution.cancellation_signalled is False


def test_remote_tool_error_is_partial_even_for_valid_text(tmp_path: Path):
    result, _ = invoke(tmp_path, [{"type": "text", "text": "remote failure"}], is_error=True)
    assert result.status == "error"
    assert result.output.bounds.partial is True
    assert isinstance(result.output, CompositeOutput)
    assert "remote failure" in composite_ordered_text(result.output)
    assert isinstance(result.execution, UnconfirmedStop)
    assert result.execution.cancellation_signalled is False


def test_resource_blob_without_mime_is_preserved_as_generic_bytes(tmp_path: Path):
    result, services = invoke(tmp_path, [{"type": "resource", "resource": {"uri": "custom://bytes", "blob": "YQ=="}}])
    assert result.status == "ok"
    artifacts = result.body.as_payload()["artifacts"]
    resource = artifacts[0]
    assert resource["media_type"] == "application/octet-stream"
    assert services.artifacts[resource["sha256"]] == b"a"


@pytest.mark.parametrize(
    "content,text",
    [
        ([{"type": "resource", "resource": {"uri": "custom://text", "text": "resource text"}}], "resource text"),
        ([{"type": "resource_link", "uri": "custom://link", "name": "reference"}], "remote resources are not fetched"),
    ],
)
def test_resource_text_and_links_need_no_binary_artifact(tmp_path: Path, content, text):
    result, services = invoke(tmp_path, content)
    assert result.status == "ok"
    assert isinstance(result.output, CompositeOutput)
    assert text in composite_ordered_text(result.output)
    assert len(services.artifacts) == 1


def test_large_binary_does_not_hide_result_reference_from_model(tmp_path: Path):
    from dataclasses import replace

    from voidcode.core.coverage import project_tool_result
    from voidcode.sdk.codec import encode_tool_result

    encoded = base64.b64encode(PNG + b"x" * 60_000).decode()
    result, services = invoke(tmp_path, [{"type": "image", "mimeType": "image/png", "data": encoded}])
    canonical = services.spill_artifact(json.dumps(encode_tool_result(result)), media_type="application/json")
    text, _, clipped = project_tool_result(replace(result, result_reference=canonical), max_bytes=2048, include_images=False)
    assert clipped is False
    reference = result.body.as_payload()["result_artifact"]
    assert f"artifact://{reference['sha256']}" in text
    assert encoded not in text
    original = json.loads(services.artifacts[reference["sha256"]])
    assert original["content"][0]["data"] == encoded


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "embedded_resource", "resource": {"uri": "custom://text", "text": "legacy"}}],
        [{"type": "resource", "uri": "custom://text", "text": "legacy"}],
        [{"type": "resource", "resource": {"uri": "custom://missing-content"}}],
    ],
)
def test_nonstandard_or_incomplete_resources_are_explicit_projection_failures(tmp_path: Path, content):
    result, services = invoke(tmp_path, content)
    assert result.status == "error"
    assert result.output.bounds.partial is True
    artifact = result.body.as_payload()["result_artifact"]
    assert json.loads(services.artifacts[artifact["sha256"]])["content"] == content
    assert isinstance(result.execution, UnconfirmedStop)


def test_mcp_multi_image_order_preserved_with_structured_json(tmp_path: Path):
    from voidcode.plugins.mcp.types import McpToolCallResult as _Result
    from voidcode.sdk.tools import ToolCall as _Call

    services = Services()
    content = [
        {"type": "text", "text": "first explanation"},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()},
        {"type": "text", "text": "middle note"},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()},
        {"type": "resource_link", "uri": "custom://link", "name": "ref"},
    ]
    structured = {"answer": 42}
    tool = McpTool(manager=Manager(_Result(content=content, structured_content=structured, is_error=False)))
    result = tool.invoke(
        _Call("mcp", {"server": "demo", "tool": "example"}),
        context=ToolContext(workspace=tmp_path, session_id="s", run_id="r", invocation_id="i", revision_id="v", actor="model"),
        services=services,  # type: ignore[arg-type]
    )
    assert result.status == "ok"
    assert isinstance(result.output, CompositeOutput)
    kinds = [type(part).__name__ for part in result.output.parts]
    assert kinds == ["TextPart", "TextPart", "MediaPart", "TextPart", "MediaPart", "TextPart", "JsonPart"]
    ordered = composite_ordered_text(result.output)
    assert ordered.index("first explanation") < ordered.index("middle note") < ordered.index("remote resources are not fetched")
    assert ordered.index("middle note") < ordered.index('"answer": 42')
    media_refs = [part.reference.sha256 for part in result.output.parts if isinstance(part, MediaPart)]
    assert len(media_refs) == 2 and media_refs[0] == media_refs[1]
    json_part = next(part for part in result.output.parts if isinstance(part, JsonPart))
    assert json.loads(services.artifacts[json_part.reference.sha256]) == structured


def test_mcp_single_small_image_legacy_bytes_still_decode(tmp_path: Path):
    from voidcode.sdk.tools import AttachmentOutput

    legacy = {
        "status": "ok",
        "tool_name": "mcp",
        "output": {"type": "attachment", "mime": "image/png", "data_uri": None, "presentation": "old", "bounds": {}},
        "body": None,
    }
    decoded = decode_tool_result(legacy)
    assert isinstance(decoded.output, AttachmentOutput)
    result, _ = invoke(tmp_path, [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()}])
    assert isinstance(result.output, CompositeOutput)
    assert any(isinstance(part, MediaPart) for part in result.output.parts)


def test_mcp_failure_diagnosis_renders_ordered_and_partial(tmp_path: Path):
    content = [
        {"type": "text", "text": "explanation"},
        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(PNG).decode()},
    ]
    result, _ = invoke(tmp_path, content, is_error=True)
    assert result.status == "error"
    assert isinstance(result.output, CompositeOutput)
    assert result.output.bounds.partial is True
    ordered = composite_ordered_text(result.output)
    assert "explanation" in ordered and "MCP image item 1" in ordered
    assert "reported an error" in result.error


def test_oversize_structured_content_fails_loudly_with_sizes(tmp_path: Path):
    from voidcode.sdk.json_values import json_wire_object
    from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

    # Multibyte content sized to straddle the byte budget: silent truncation would
    # drop a split character instead of failing.
    big = {"blob": "é" * 40_000}
    actual = len(json.dumps(json_wire_object(big), ensure_ascii=False, sort_keys=True).encode("utf-8"))
    assert actual > MAX_VIEW_DATA_BYTES
    services = Services()
    tool = McpTool(manager=Manager(McpToolCallResult(content=[{"type": "text", "text": "note"}], structured_content=big, is_error=False)))
    result = tool.invoke(
        ToolCall("mcp", {"server": "demo", "tool": "example"}),
        context=ToolContext(workspace=tmp_path, session_id="s", run_id="r", invocation_id="i", revision_id="v", actor="model"),
        services=services,  # type: ignore[arg-type]
    )
    assert result.status == "error"
    assert isinstance(result.output, CompositeOutput)
    assert result.output.bounds.partial is True
    assert result.error == (
        f"MCP content projection failed: MCP structured content preview exceeds {MAX_VIEW_DATA_BYTES} bytes (actual size {actual} bytes)"
    )
    assert result.body.as_payload()["projection_error"].startswith("MCP structured content preview exceeds")
    artifact = result.body.as_payload()["result_artifact"]
    assert json.loads(services.artifacts[artifact["sha256"]])["structured_content"] == big


def test_post_send_timeout_returns_unconfirmed_stop(tmp_path: Path):
    error = McpCallError(
        "MCP server timed out after 30.0s. The server may be unresponsive.",
        boundary="in_flight",
        request_may_have_started=True,
        timed_out=True,
    )
    result, _ = invoke_manager(tmp_path, FailingManager(error))
    assert result.status == "error"
    assert isinstance(result.execution, UnconfirmedStop)
    assert result.execution.cancellation_signalled is False
    assert result.body.as_payload()["boundary"] == "in_flight"
    assert result.body.as_payload()["timed_out"] is True


def test_post_send_cancellation_returns_unconfirmed_stop(tmp_path: Path):
    error = McpCallError(
        "MCP call was cancelled after the request may have reached the server",
        boundary="cancelled",
        request_may_have_started=True,
        cancelled=True,
    )
    result, _ = invoke_manager(tmp_path, FailingManager(error))
    assert result.status == "error"
    assert isinstance(result.execution, UnconfirmedStop)
    assert result.execution.cancellation_signalled is True
    assert result.body.as_payload()["cancelled"] is True


def test_pre_send_manager_failure_returns_confirmed_stop(tmp_path: Path):
    error = McpCallError(
        "MCP[demo/example]: request was not sent",
        boundary="pre_send",
        request_may_have_started=False,
    )
    result, _ = invoke_manager(tmp_path, FailingManager(error))
    assert result.status == "error"
    assert isinstance(result.execution, ConfirmedStop)
    assert result.execution.cancellation_signalled is False
    assert result.body.as_payload()["request_may_have_started"] is False


def test_abort_signal_is_forwarded_to_the_manager(tmp_path: Path):
    signal = object()
    manager = RecordingManager()
    result, _ = invoke_manager(tmp_path, manager, abort_signal=signal)
    assert result.status == "ok"
    assert manager.abort_signal is signal


def test_invalid_result_json_is_an_unconfirmed_stop(tmp_path: Path):
    result, _ = invoke_manager(tmp_path, Manager(McpToolCallResult(content=[{"type": object()}])))
    assert result.status == "error"
    assert isinstance(result.execution, UnconfirmedStop)
    assert result.execution.cancellation_signalled is False
