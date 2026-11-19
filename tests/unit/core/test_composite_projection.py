"""Composite model projection, history/compaction order, and commit backstop."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.png import rgb_png
from voidcode.bootstrap import open_core
from voidcode.core.coverage import project_tool_result, read_artifact_content
from voidcode.core.history_renderer import render_event_block
from voidcode.plugins.compaction.projection import _output_text
from voidcode.sdk.codec import decode_tool_result, encode_tool_result
from voidcode.sdk.services import ArtifactPage, ArtifactReference
from voidcode.sdk.tools import (
    TOOL_OUTPUT_BYTES,
    CompositeOutput,
    JsonPart,
    MediaPart,
    OpaqueToolBody,
    OutputBounds,
    TablePart,
    TextPart,
    ToolDiagnostics,
    ToolFailure,
    ToolSuccess,
    composite_ordered_text,
)


class _Services:
    def __init__(self, tmp_path: Path) -> None:
        from voidcode.core.artifacts import ArtifactStore

        self.store = ArtifactStore(tmp_path)

    def spill(self, data: bytes, media_type: str) -> ArtifactReference:
        identity, spill = self.store.start_spill(media_type, owner="test")
        try:
            spill.append(data)
            digest, size, media = self.store.finish(identity)
        except BaseException:
            self.store.discard(identity)
            raise
        return ArtifactReference(digest, size, media)

    def page(self, digest: str, offset: int, limit: int) -> ArtifactPage:
        data, total, eof, media = self.store.read(digest, offset=offset, limit=limit)
        return ArtifactPage(ArtifactReference(digest, total, media), offset, data, eof)


def _canonical(services: _Services, result):  # type: ignore[no-untyped-def]
    from dataclasses import replace

    original = replace(result, result_reference=None)
    reference = services.spill(
        json.dumps(encode_tool_result(original), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
        "application/vnd.voidcode.tool-result+json",
    )
    return replace(original, result_reference=reference)


def test_composite_projection_orders_text_and_two_images(tmp_path: Path) -> None:
    services = _Services(tmp_path)
    png_a = services.spill(rgb_png(4, 4), "image/png")
    png_b = services.spill(rgb_png(4, 5), "image/png")
    json_ref = services.spill(b'{"ok":true}', "application/json")
    output = CompositeOutput(
        parts=(
            TextPart("explanation here"),
            TablePart(columns=("a", "b"), rows=(("1", "2"), ("3", "4"))),
            MediaPart(reference=png_a, mime="image/png", presentation="first chart"),
            MediaPart(reference=png_b, mime="image/png", presentation="second chart"),
            JsonPart(reference=json_ref, preview='{"ok":true}'),
        ),
        presentation="overall summary",
    )
    result = _canonical(services, ToolSuccess("mcp", output=output, body=OpaqueToolBody({"k": "v"})))
    text, images, clipped = project_tool_result(result, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=services.page)
    document = json.loads(text)
    assert document["body_omitted"] is True
    assert "body" not in document
    body_text: str = document["text"]
    assert body_text.index("explanation here") < body_text.index("a\tb") < body_text.index("first chart")
    assert body_text.index("first chart") < body_text.index("second chart") < body_text.index('{"ok":true}')
    assert document["presentation"] == "overall summary"
    assert [part["kind"] for part in document["parts"]] == ["text", "table", "media", "media", "json"]
    assert len(document["output_uris"]) == 3
    assert len(images) == 2
    assert images[0].artifact_id == png_a.sha256
    assert images[1].artifact_id == png_b.sha256
    assert images[0].data not in text
    assert clipped is False


def test_composite_projection_requires_reader_only_for_raster(tmp_path: Path) -> None:
    services = _Services(tmp_path)
    png = services.spill(rgb_png(2, 2), "image/png")
    audio = services.spill(b"RIFF....", "audio/wav")
    raster = _canonical(services, ToolSuccess("m", output=CompositeOutput(parts=(MediaPart(reference=png, mime="image/png", presentation="pic"),))))
    try:
        project_tool_result(raster, max_bytes=4096)
    except ValueError as exc:
        assert "artifact page reader" in str(exc)
    else:
        raise AssertionError("expected reader requirement")
    text_only, images, _ = project_tool_result(raster, max_bytes=4096, include_images=False)
    assert images == ()
    assert "pic" in text_only
    non_raster = ToolSuccess("m", output=CompositeOutput(parts=(MediaPart(reference=audio, mime="audio/wav", presentation="clip"),)))
    text, images, _ = project_tool_result(non_raster, max_bytes=4096)
    assert images == ()
    assert "clip" in text and audio.uri in text


def test_composite_failure_keeps_error_and_partial(tmp_path: Path) -> None:
    services = _Services(tmp_path)
    output = CompositeOutput(parts=(TextPart("partial output"),), bounds=OutputBounds(partial=True))
    raw = ToolFailure(
        "mcp",
        error="boom happened",
        output=output,
        diagnostics=ToolDiagnostics(kind="k", summary="s", guidance="retry"),
    )
    result = _canonical(services, raw)
    text, _, clipped = project_tool_result(result, max_bytes=TOOL_OUTPUT_BYTES, artifact_reader=services.page)
    document = json.loads(text)
    assert document["status"] == "error"
    assert document["error"] == "boom happened"
    assert document["partial"] is True
    assert "partial output" in document["text"]
    assert clipped is False


def test_history_and_compaction_render_composite_in_order() -> None:
    ref = ArtifactReference("0" * 64, 10, "image/png")
    json_ref = ArtifactReference("1" * 64, 12, "application/json")
    output = CompositeOutput(
        parts=(
            TextPart("first"),
            TablePart(columns=("c",), rows=(("v",),)),
            MediaPart(reference=ref, mime="image/png", presentation="alt text"),
            JsonPart(reference=json_ref, preview='{"a":1}'),
        )
    )
    ordered = composite_ordered_text(output)
    assert ordered.index("first") < ordered.index("c\nv") < ordered.index("alt text") < ordered.index('{"a":1}')
    assert _output_text(output) == ordered

    class _Event:
        sequence = 9
        event_type = "core.tool_completed"
        source = "core"
        payload: Mapping[str, object] | None = {"result": encode_tool_result(ToolSuccess("m", output=output))}

    block = render_event_block(_Event())  # type: ignore[arg-type]
    assert block is not None
    assert block.index("first") < block.index("alt text")

    failed = ToolFailure("m", error="bad", output=output)

    class _FailedEvent:
        sequence = 10
        event_type = "core.tool_completed"
        source = "core"
        payload: Mapping[str, object] | None = {"result": encode_tool_result(failed)}

    failed_block = render_event_block(_FailedEvent())  # type: ignore[arg-type]
    assert failed_block is not None
    assert "bad" in failed_block and "first" in failed_block


def test_oversize_inline_table_compacts_to_reference_prefix_without_loss(tmp_path: Path) -> None:
    columns = ("id", "name", "value")
    rows = tuple((str(i), f"name-{i}", "x" * 20) for i in range(2000))
    output = CompositeOutput(parts=(TablePart(columns=columns, rows=rows),))
    result = ToolSuccess("mcp", output=output)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    with open_core(tmp_path / "workspace", state_dir=tmp_path / "state") as core:
        compact, size, clipped = core._prepare_report_result(result)
        assert size > TOOL_OUTPUT_BYTES
        assert clipped is True
        assert isinstance(compact.output, CompositeOutput)
        assert len(compact.output.parts) == 1
        part = compact.output.parts[0]
        assert isinstance(part, TablePart)
        assert part.reference is not None
        assert 0 < len(part.rows) < len(rows)
        assert part.rows == rows[: len(part.rows)]
        assert part.total_rows == len(rows)
        assert compact.output.bounds.partial is True
        assert compact.result_reference is not None
        full_payload = json.loads(read_artifact_content(part.reference, core._artifact_page).decode("utf-8"))
        assert full_payload["columns"] == list(columns)
        assert full_payload["rows"] == [list(row) for row in rows]
        assert full_payload["total_rows"] == len(rows)
        canonical = json.loads(read_artifact_content(compact.result_reference, core._artifact_page).decode("utf-8"))
        canonical_rows = canonical["output"]["parts"][0]["rows"]
        assert canonical_rows == [list(row) for row in rows]
        compact_bytes = len(
            json.dumps(encode_tool_result(compact), ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        )
        assert compact_bytes <= TOOL_OUTPUT_BYTES
        decoded = decode_tool_result(encode_tool_result(compact))
        assert isinstance(decoded.output, CompositeOutput)


def test_small_table_passes_through_without_spill(tmp_path: Path) -> None:
    output = CompositeOutput(parts=(TablePart(columns=("a",), rows=(("1",), ("2",))),))
    result = ToolSuccess("mcp", output=output)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    with open_core(tmp_path / "workspace", state_dir=tmp_path / "state") as core:
        compact, size, clipped = core._prepare_report_result(result)
        assert size <= TOOL_OUTPUT_BYTES
        assert clipped is False
        assert isinstance(compact.output, CompositeOutput)
        part = compact.output.parts[0]
        assert isinstance(part, TablePart)
        assert part.reference is None
        assert part.rows == (("1",), ("2",))


def test_openai_tool_images_use_synthetic_in_order_unknown_stays_unknown() -> None:
    import pytest

    from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
    from voidcode.sdk.provider import ImageAttachment, ProviderMessage, ProviderTurnRequest
    from voidcode.sdk.tools import ToolCall, ToolDefinition

    images = (
        ImageAttachment(media_type="image/png", data="Zmlyc3Q=", artifact_id="a" * 64),
        ImageAttachment(media_type="image/png", data="c2Vjb25k", artifact_id="b" * 64),
    )
    call = ToolCall(tool_name="mcp", arguments={}, tool_call_id="call-1")
    request = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="user", content="go"),
            ProviderMessage(role="assistant", content=None, tool_calls=(call,)),
            ProviderMessage(role="tool", content='{"status":"ok"}', tool_call_id="call-1", tool_name="mcp", images=images),
        ),
        available_tools=(ToolDefinition(name="mcp", description="d", input_schema={}),),
        provider_name="endpoint",
        model_name="unknown-model",
    )
    provider = OpenAIChatCompletionsProvider()
    wire = provider._messages(request)
    assert all(message["role"] != "tool" for message in wire)
    synthetic = next(message for message in wire if message["role"] == "user" and isinstance(message["content"], list))
    content = synthetic["content"]
    assert isinstance(content, list)
    urls = [block["image_url"]["url"] for block in content if isinstance(block, dict) and block.get("type") == "image_url"]
    assert urls == ["data:image/png;base64,Zmlyc3Q=", "data:image/png;base64,c2Vjb25k"]
    text_block = next(block for block in content if isinstance(block, dict) and block.get("type") == "text")
    payload = json.loads(text_block["text"])
    assert payload["tool_name"] == "mcp" and payload["tool_call_id"] == "call-1"

    known_text_only = ProviderTurnRequest(
        messages=(ProviderMessage(role="tool", content="x", tool_call_id="c", tool_name="m", images=images[:1]),),
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
    )
    with pytest.raises(Exception, match="does not support image input"):
        provider._payload(known_text_only, stream=False)
