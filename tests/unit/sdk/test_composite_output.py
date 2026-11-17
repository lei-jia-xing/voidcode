"""Composite output + table/json/media view contracts."""

from __future__ import annotations

import json

import pytest

from voidcode.sdk.codec import decode_tool_result, encode_tool_result
from voidcode.sdk.resources import ResourcePage
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import (
    AttachmentOutput,
    CompositeOutput,
    EmptyOutput,
    JsonPart,
    MediaPart,
    OpaqueToolBody,
    TablePart,
    TextOutput,
    TextPart,
    ToolFailure,
    ToolSuccess,
)
from voidcode.sdk.views import (
    JsonViewData,
    MediaViewData,
    TableViewData,
    validate_view_data,
)


def _ref(mime: str = "image/png") -> ArtifactReference:
    return ArtifactReference("0" * 64, 10, mime)


def test_composite_codec_roundtrip_preserves_order() -> None:
    output = CompositeOutput(
        parts=(
            TextPart("explanation"),
            TablePart(columns=("a", "b"), rows=(("1", "2"), ("3", "4"))),
            MediaPart(reference=_ref(), mime="image/png", presentation="first chart"),
            MediaPart(reference=_ref("image/jpeg"), mime="image/jpeg", presentation="second chart"),
            JsonPart(reference=_ref("application/json"), preview='{"ok": true}'),
        ),
        presentation="summary",
    )
    result = ToolSuccess("mcp", output=output, body=OpaqueToolBody({"k": "v"}))
    payload = encode_tool_result(result)
    encoded_output = payload["output"]
    assert isinstance(encoded_output, dict)
    assert encoded_output["type"] == "composite"
    encoded_parts = encoded_output["parts"]
    assert isinstance(encoded_parts, list)
    assert [part["kind"] for part in encoded_parts if isinstance(part, dict)] == ["text", "table", "media", "media", "json"]
    decoded = decode_tool_result(payload)
    assert isinstance(decoded, ToolSuccess)
    assert isinstance(decoded.output, CompositeOutput)
    assert [type(part).__name__ for part in decoded.output.parts] == [
        "TextPart",
        "TablePart",
        "MediaPart",
        "MediaPart",
        "JsonPart",
    ]
    assert decoded.output.presentation == "summary"


def test_legacy_single_variant_wire_bytes_unchanged() -> None:
    text = ToolSuccess("t", output=TextOutput("hi", presentation="p"))
    empty = ToolSuccess("t", output=EmptyOutput(presentation="e"))
    attach = ToolSuccess("t", output=AttachmentOutput("image/png", None, presentation="img"))
    for result in (text, empty, attach):
        payload = encode_tool_result(result)
        encoded_output = payload["output"]
        assert isinstance(encoded_output, dict)
        assert encoded_output["type"] in {"text", "empty", "attachment"}
        assert "parts" not in encoded_output
        assert "kind" not in json.dumps(encoded_output)
        assert type(decode_tool_result(payload).output) is type(result.output)
    legacy = {"status": "ok", "tool_name": "t", "output": {"type": "text", "text": "old", "bounds": {}}, "body": None}
    decoded = decode_tool_result(legacy)
    assert isinstance(decoded.output, TextOutput)
    assert decoded.output.text == "old"


def test_composite_rejects_empty_and_unknown_part() -> None:
    with pytest.raises(ValueError, match="at least one part"):
        CompositeOutput(parts=())
    with pytest.raises(ValueError, match="invalid type"):
        CompositeOutput(parts=("nope",))  # type: ignore[arg-type]


def test_table_truncation_requires_reference() -> None:
    with pytest.raises(ValueError, match="truncated table requires a reference"):
        TablePart(columns=("a",), rows=(("1",),), total_rows=5)
    ok = TablePart(columns=("a",), rows=(("1",),), total_rows=5, reference=_ref("application/json"))
    assert ok.total_rows == 5
    with pytest.raises(ValueError, match="row length must match"):
        TablePart(columns=("a", "b"), rows=(("only-one",),))
    with pytest.raises(ValueError, match="at least one column"):
        TablePart(columns=(), rows=())


def test_media_and_json_require_readable_fallback() -> None:
    with pytest.raises(ValueError, match="presentation must be a non-empty string"):
        MediaPart(reference=_ref(), mime="image/png", presentation="")
    with pytest.raises(ValueError, match="preview must be a non-empty string"):
        JsonPart(reference=_ref("application/json"), preview="")
    with pytest.raises(ValueError, match="requires an artifact reference"):
        MediaPart(reference="artifact://abc", mime="image/png", presentation="x")  # type: ignore[arg-type]


def test_resource_page_accepts_composite() -> None:
    page = ResourcePage(output=CompositeOutput(parts=(TextPart("hi"),)), body={"type": "composite"})
    assert isinstance(page.output, CompositeOutput)


def test_table_json_media_views_roundtrip() -> None:
    table = TableViewData.from_payload({"columns": ["a"], "rows": [["1"], ["2"]], "total_rows": 2})
    assert table.columns == ("a",)
    assert table.rows == (("1",), ("2",))
    media_payload = {"reference": {"sha256": "0" * 64, "bytes": 10, "media_type": "image/png"}, "mime": "image/png", "alt": "chart"}
    media = MediaViewData.from_payload(media_payload)
    assert media.alt == "chart"
    json_view = JsonViewData.from_payload({"preview": '{"a": 1}'})
    assert json_view.reference is None
    validated = validate_view_data("table", {"columns": ["a"], "rows": [["1"]]})
    assert validated["columns"] == ("a",) or validated["columns"] == ["a"]


def test_view_truncation_and_bounds_fail_visibly() -> None:
    with pytest.raises(ValueError, match="truncated table requires a reference"):
        TableViewData.from_payload({"columns": ["a"], "rows": [["1"]], "total_rows": 9})
    with pytest.raises(ValueError, match="alt must be a non-empty string"):
        MediaViewData.from_payload({"reference": {"sha256": "0" * 64, "bytes": 1, "media_type": "image/png"}, "mime": "image/png", "alt": ""})
    big = "x" * (64 * 1024 + 1)
    with pytest.raises(ValueError, match="exceeds"):
        validate_view_data("table", {"columns": ["a"], "rows": [[big]]})


def test_composite_failure_preserves_diagnosis() -> None:
    output = CompositeOutput(parts=(TextPart("output"),), presentation="summary")
    result = ToolFailure("mcp", error="boom", output=output)
    decoded = decode_tool_result(encode_tool_result(result))
    assert isinstance(decoded, ToolFailure)
    assert decoded.error == "boom"
    assert isinstance(decoded.output, CompositeOutput)
