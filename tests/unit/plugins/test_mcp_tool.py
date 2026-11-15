"""MCP content projection preserves full artifacts and honest failure bounds."""

import base64
import json
from hashlib import sha256
from pathlib import Path

import pytest

from voidcode.plugins.mcp.mcp_tool import McpTool
from voidcode.plugins.mcp.types import McpToolCallResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import AttachmentOutput, TextOutput, ToolCall

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=")


class Manager:
    def __init__(self, result):
        self.result = result

    def call_tool(self, **kwargs):
        return self.result

    def descriptor_for(self, **kwargs):
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


def test_empty_content_still_references_the_original_json(tmp_path: Path):
    result, services = invoke(tmp_path, [])
    assert result.status == "ok"
    assert isinstance(result.output, TextOutput)
    reference = result.output.bounds.reference
    assert reference is not None
    assert json.loads(services.artifacts[reference.sha256]) == {"content": [], "structured_content": None, "is_error": False}
    assert reference.uri in result.output.text


@pytest.mark.parametrize("size,attachment", [(0, True), (60_000, False)])
def test_image_projection_preserves_binary_and_json_references(tmp_path: Path, size: int, attachment: bool):
    data = PNG + b"x" * size
    result, services = invoke(tmp_path, [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(data).decode()}])
    assert result.status == "ok"
    assert isinstance(result.output, AttachmentOutput if attachment else TextOutput)
    assert services.artifacts[sha256(data).hexdigest()] == data
    assert result.output.bounds.reference is not None
    expected_mime = "image/png" if attachment else "application/json"
    assert result.output.bounds.reference.media_type == expected_mime
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


def test_remote_tool_error_is_partial_even_for_valid_text(tmp_path: Path):
    result, _ = invoke(tmp_path, [{"type": "text", "text": "remote failure"}], is_error=True)
    assert result.status == "error"
    assert result.output.bounds.partial is True
    assert "remote failure" in result.output.text


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
    assert text in result.output.text
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
