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
        compact, size, clipped = core._projection.prepare_report(result)
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
        full_payload = json.loads(read_artifact_content(part.reference, core.artifact_page).decode("utf-8"))
        assert full_payload["columns"] == list(columns)
        assert full_payload["rows"] == [list(row) for row in rows]
        assert full_payload["total_rows"] == len(rows)
        canonical = json.loads(read_artifact_content(compact.result_reference, core.artifact_page).decode("utf-8"))
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
        compact, size, clipped = core._projection.prepare_report(result)
        assert size <= TOOL_OUTPUT_BYTES
        assert clipped is False
        assert isinstance(compact.output, CompositeOutput)
        part = compact.output.parts[0]
        assert isinstance(part, TablePart)
        assert part.reference is None
        assert part.rows == (("1",), ("2",))


def test_openai_tool_images_keep_tool_structure_and_feedback_once() -> None:
    from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
    from voidcode.sdk.provider import ImageAttachment, ProviderMessage, ProviderTurnRequest
    from voidcode.sdk.tools import ToolCall, ToolDefinition

    images_a = (
        ImageAttachment(media_type="image/png", data="Zmlyc3Q=", artifact_id="a" * 64),
        ImageAttachment(media_type="image/gif", data="c2Vjb25k", artifact_id="b" * 64),
    )
    images_b = (ImageAttachment(media_type="image/webp", data="dGhpcmQ=", artifact_id="c" * 64),)
    calls = (
        ToolCall(tool_name="mcp", arguments={}, tool_call_id="call-1"),
        ToolCall(tool_name="read", arguments={"path": "b"}, tool_call_id="call-2"),
        ToolCall(tool_name="read", arguments={"path": "c"}, tool_call_id="call-3"),
    )
    request = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="user", content="go"),
            ProviderMessage(role="assistant", content="planning", tool_calls=calls),
            ProviderMessage(role="tool", content="first body", tool_call_id="call-1", tool_name="mcp", images=images_a),
            ProviderMessage(role="tool", content="text only", tool_call_id="call-2", tool_name="read"),
            ProviderMessage(role="tool", content="third body", tool_call_id="call-3", tool_name="read", images=images_b),
        ),
        available_tools=(ToolDefinition(name="mcp", description="d", input_schema={}),),
        provider_name="endpoint",
        model_name="unknown-model",
    )
    provider = OpenAIChatCompletionsProvider()
    wire = provider._messages(request)

    assert [message["role"] for message in wire] == ["user", "assistant", "tool", "tool", "tool", "user"]
    assistant = wire[1]
    assistant_calls = assistant["tool_calls"]
    assert isinstance(assistant_calls, list)
    assert [(call["id"], call["function"]["name"]) for call in assistant_calls] == [
        ("call-1", "mcp"),
        ("call-2", "read"),
        ("call-3", "read"),
    ]

    tool_messages = wire[2:5]
    assert [message["tool_call_id"] for message in tool_messages] == ["call-1", "call-2", "call-3"]
    assert [message["content"] for message in tool_messages] == ["first body", "text only", "third body"]
    assert all("images" not in message for message in tool_messages)

    feedback = wire[-1]
    assert feedback["role"] == "user"
    content = feedback["content"]
    assert isinstance(content, list)
    text_blocks = [block["text"] for block in content if block.get("type") == "text"]
    assert len(text_blocks) == 2
    assert "call-1" in text_blocks[0] and "mcp" in text_blocks[0] and "Tool output" in text_blocks[0]
    assert "call-3" in text_blocks[1] and "read" in text_blocks[1] and "Tool output" in text_blocks[1]
    urls = [block["image_url"]["url"] for block in content if block.get("type") == "image_url"]
    assert urls == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/gif;base64,c2Vjb25k",
        "data:image/webp;base64,dGhpcmQ=",
    ]

    known_text_only = ProviderTurnRequest(
        messages=(ProviderMessage(role="tool", content="x", tool_call_id="c", tool_name="m", images=images_a[:1]),),
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
    )
    import pytest

    with pytest.raises(Exception, match="does not support image input"):
        provider._payload(known_text_only, stream=False)


def test_openai_tool_image_feedback_deduplicates_repeated_images() -> None:
    from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
    from voidcode.sdk.provider import ImageAttachment, ProviderMessage, ProviderTurnRequest
    from voidcode.sdk.tools import ToolCall

    image = ImageAttachment(media_type="image/png", data="c2FtZQ==", artifact_id="same" * 16)
    calls = (
        ToolCall(tool_name="read", arguments={"path": "a"}, tool_call_id="call-1"),
        ToolCall(tool_name="read", arguments={"path": "b"}, tool_call_id="call-2"),
    )
    request = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="assistant", content=None, tool_calls=calls),
            ProviderMessage(role="tool", content="a", tool_call_id="call-1", tool_name="read", images=(image, image)),
            ProviderMessage(role="tool", content="b", tool_call_id="call-2", tool_name="read", images=(image,)),
        ),
        provider_name="endpoint",
        model_name="unknown-model",
    )
    feedback = OpenAIChatCompletionsProvider()._messages(request)[-1]

    content = feedback["content"]
    assert isinstance(content, list)
    image_blocks = [block for block in content if block.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"] == "data:image/png;base64,c2FtZQ=="


def test_openai_text_only_tool_history_is_unchanged() -> None:
    from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
    from voidcode.sdk.provider import ProviderMessage, ProviderTurnRequest
    from voidcode.sdk.tools import ToolCall

    request = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="user", content="go"),
            ProviderMessage(
                role="assistant",
                content=None,
                tool_calls=(ToolCall(tool_name="read", arguments={"path": "a"}, tool_call_id="call-1"),),
            ),
            ProviderMessage(role="tool", content="body", tool_call_id="call-1", tool_name="read"),
        ),
        provider_name="endpoint",
        model_name="unknown-model",
    )
    wire = OpenAIChatCompletionsProvider()._messages(request)

    assert [message["role"] for message in wire] == ["user", "assistant", "tool"]
    assert wire[2] == {"role": "tool", "tool_call_id": "call-1", "content": "body"}


def test_prepare_report_compacts_oversized_composite_with_long_presentation(tmp_path: Path) -> None:
    from voidcode.core.output_projection import ResultProjector

    services = _Services(tmp_path)
    projector = ResultProjector(services.store)
    raw_text = "alpha-" + "x" * (TOOL_OUTPUT_BYTES + 12000)
    raw = ToolSuccess(
        "big_tool",
        output=CompositeOutput(
            parts=(TextPart(raw_text),),
            presentation="summary-" + "p" * 20000,
        ),
        body=OpaqueToolBody({"huge": "b" * 10000}),
    )
    compact, raw_size, clipped = projector.prepare_report(raw)
    assert raw_size > TOOL_OUTPUT_BYTES
    assert clipped is True
    encoded_bytes = len(json.dumps(encode_tool_result(compact), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert encoded_bytes <= TOOL_OUTPUT_BYTES
    assert compact.body is None
    assert compact.result_reference is not None
    assert isinstance(compact.output, CompositeOutput)
    assert compact.output.bounds.partial is True
    part = compact.output.parts[0]
    assert isinstance(part, TextPart)
    assert len(part.text) < len(raw_text)
    assert part.reference is not None
    raw_data, _, _, _ = services.store.read(part.reference.sha256, offset=0, limit=len(raw_text) + 1024)
    assert raw_data.decode("utf-8") == raw_text
