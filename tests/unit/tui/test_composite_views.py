"""Composite tool results and table/json/media views render ordered with explicit fallbacks."""

from __future__ import annotations

import pytest

from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.views import JsonViewData, MediaViewData, PluginViewDescriptor, TableViewData
from voidcode.tui.artifact_viewer import artifact_sources
from voidcode.tui.styles import Styles
from voidcode.tui.views import (
    render_json,
    render_media,
    render_plugin_view,
    render_table,
    render_tool_result,
)

PNG_REF = {"sha256": "a" * 64, "bytes": 128, "media_type": "image/png"}
JPG_REF = {"sha256": "b" * 64, "bytes": 256, "media_type": "image/jpeg"}
JSON_REF = {"sha256": "c" * 64, "bytes": 12, "media_type": "application/json"}


def _composite_result(status: str = "ok", **extra: object) -> dict[str, object]:
    return {
        "status": status,
        "output": {
            "type": "composite",
            "parts": [
                {"kind": "text", "text": "explanation here"},
                {"kind": "table", "columns": ["a", "b"], "rows": [["1", "2"], ["3", "4"]]},
                {"kind": "media", "reference": PNG_REF, "mime": "image/png", "presentation": "first chart"},
                {"kind": "media", "reference": JPG_REF, "mime": "image/jpeg", "presentation": "second chart"},
                {"kind": "json", "reference": JSON_REF, "preview": '{"ok": true}'},
            ],
            "bounds": {},
        },
        **extra,
    }


def test_composite_renders_ordered_with_exact_degraded_strings() -> None:
    rows = render_tool_result(_composite_result(), 200, Styles(None))
    text = "\n".join(rows)
    flat = " ".join(text.split())
    assert text.index("explanation here") < text.index("first chart")
    assert text.index("first chart") < text.index("second chart") < text.index('{"ok": true}')
    assert "a b" in flat and "1 2" in flat and "3 4" in flat
    assert flat.index("explanation here") < flat.index("a b") < flat.index("first chart")
    assert "[Image: image/png, 128 bytes, artifact://" + "a" * 64 + "] first chart" in text
    assert "[Image: image/jpeg, 256 bytes, artifact://" + "b" * 64 + "] second chart" in text
    assert "[full JSON: artifact://" + "c" * 64 + "]" in text


def test_composite_failure_shows_parts_then_error() -> None:
    rows = render_tool_result(_composite_result("error", error="boom happened"), 120, Styles(None))
    text = "\n".join(rows)
    assert "explanation here" in text
    assert "Error: boom happened" in text
    assert text.index("second chart") < text.index("Error: boom happened")


def test_cancelled_composite_partial_never_renders_as_success() -> None:
    result = _composite_result("error", error="User aborted the command.")
    output = result["output"]
    assert isinstance(output, dict)
    output["bounds"] = {"partial": True}
    rows = render_tool_result(result, 120, Styles(None), interrupted=True)
    text = "\n".join(rows)
    assert "explanation here" in text
    assert "Interrupted: User aborted the command." in text
    assert "Error:" not in text
    assert rows[-1] == "[partial]"


def test_composite_part_references_offer_artifact_paging() -> None:
    rows = render_tool_result(_composite_result(), 120, Styles(None))
    assert rows[-1].endswith("Artifacts: Ctrl+O / /tui output · original sources and structured result are separate")


def test_audio_and_generic_media_labels_are_explicit() -> None:
    result = {
        "status": "ok",
        "output": {
            "type": "composite",
            "parts": [
                {
                    "kind": "media",
                    "reference": {"sha256": "d" * 64, "bytes": 10, "media_type": "audio/wav"},
                    "mime": "audio/wav",
                    "presentation": "clip",
                },
                {
                    "kind": "media",
                    "reference": {"sha256": "e" * 64, "bytes": 4, "media_type": "application/octet-stream"},
                    "mime": "application/octet-stream",
                    "presentation": "blob",
                },
            ],
            "bounds": {},
        },
    }
    text = "\n".join(render_tool_result(result, 200, Styles(None)))
    assert "[Audio: audio/wav, 10 bytes, artifact://" + "d" * 64 + "] clip" in text
    assert "[Media: application/octet-stream, 4 bytes, artifact://" + "e" * 64 + "] blob" in text


def test_table_caption_truncation_and_text_reference_notes() -> None:
    result = {
        "status": "ok",
        "output": {
            "type": "composite",
            "parts": [
                {
                    "kind": "table",
                    "columns": ["id"],
                    "rows": [["1"]],
                    "total_rows": 9,
                    "reference": JSON_REF,
                    "presentation": "Top rows",
                },
                {"kind": "text", "text": "prefix", "reference": JSON_REF},
            ],
            "bounds": {"truncated": True},
        },
    }
    rows = render_tool_result(result, 120, Styles(None))
    text = "\n".join(rows)
    assert "Top rows\nid\n1\n[showing 1/9 rows; full table: artifact://" + "c" * 64 + "]" in text
    assert "prefix\n[full text: artifact://" + "c" * 64 + "]" in text
    assert rows[-1] == "[truncated]"


def test_malformed_parts_render_explicit_markers_without_crashing() -> None:
    result = {
        "status": "ok",
        "output": {
            "type": "composite",
            "parts": [
                {"kind": "mystery", "text": "x"},
                {"kind": "media", "mime": "image/png"},
                {"kind": "table", "columns": [], "rows": []},
                {"kind": "json"},
            ],
            "bounds": {},
        },
    }
    text = "\n".join(render_tool_result(result, 120, Styles(None)))
    assert "[unknown part kind: 'mystery']" in text
    assert "[Image: image/png, unknown bytes, missing-uri] (missing presentation)" in text
    assert "[table: missing columns]" in text
    assert "(missing preview)\n[full JSON: missing-uri]" in text


def test_terminal_controls_in_parts_are_escaped() -> None:
    result = {
        "status": "ok",
        "output": {
            "type": "composite",
            "parts": [
                {"kind": "text", "text": "hi\x1b[2J"},
                {"kind": "media", "reference": PNG_REF, "mime": "image/png", "presentation": "alt\x07"},
            ],
            "bounds": {},
        },
    }
    text = "\n".join(render_tool_result(result, 120, Styles(None)))
    assert "\x1b" not in text and "\x07" not in text
    assert "hi\\x1b[2J" in text


def test_artifact_sources_discovers_part_references_in_order() -> None:
    result = _composite_result()
    result["result_reference"] = {"sha256": "f" * 64, "bytes": 99, "media_type": "application/json"}
    sources = artifact_sources(result)
    assert [source.label for source in sources] == [
        "Part 2 (media)",
        "Part 3 (media)",
        "Part 4 (json)",
        "Complete structured result JSON",
    ]
    assert [source.sha256 for source in sources] == ["a" * 64, "b" * 64, "c" * 64, "f" * 64]
    assert sources[0].reference is not None and sources[0].reference.bytes == 128


def test_artifact_sources_dedups_bounds_reference_before_parts() -> None:
    result = _composite_result()
    output = result["output"]
    assert isinstance(output, dict)
    output["bounds"] = {"reference": PNG_REF}
    sources = artifact_sources(result)
    assert sources[0].label == "Original output source"
    assert [source.sha256 for source in sources].count("a" * 64) == 1


def test_table_view_renders_header_rows_and_truncation_note() -> None:
    data = TableViewData(
        columns=("id", "name"), rows=(("1", "ada"), ("2", "bob")), total_rows=7, reference=ArtifactReference("c" * 64, 90, "application/json")
    )
    text = "\n".join(render_table(data, 80, Styles(None)))
    assert "id" in text and "ada" in text and "bob" in text
    assert "[showing 2/7 rows; full table: artifact://" + "c" * 64 + "]" in text


def test_json_and_media_views_render_preview_and_placeholder() -> None:
    json_text = "\n".join(
        render_json(JsonViewData(preview='{"a": 1}', reference=ArtifactReference("c" * 64, 12, "application/json")), 80, Styles(None))
    )
    assert '{"a": 1}' in json_text
    assert "[full JSON: artifact://" + "c" * 64 + "]" in json_text
    media_text = "\n".join(
        render_media(MediaViewData(reference=ArtifactReference("a" * 64, 128, "image/png"), mime="image/png", alt="chart"), 200, Styles(None))
    )
    assert "[Image: image/png, 128 bytes, artifact://" + "a" * 64 + "] chart" in media_text


def test_plugin_view_dispatch_covers_eleven_kinds() -> None:
    table = PluginViewDescriptor(owner="t", name="t", title="T", view_type="table", data={"columns": ["a"], "rows": [["1"]]})
    assert "a" in "\n".join(render_plugin_view(table, 60, Styles(None)))
    media = PluginViewDescriptor(
        owner="t",
        name="m",
        title="M",
        view_type="media",
        data={"reference": {"sha256": "a" * 64, "bytes": 1, "media_type": "image/png"}, "mime": "image/png", "alt": "x"},
    )
    assert "[Image:" in "\n".join(render_plugin_view(media, 100, Styles(None)))
    broken = PluginViewDescriptor(owner="t", name="n", title="N", view_type="notice", data={"message": "hi", "level": "info"})
    object.__setattr__(broken, "view_type", "mystery")
    with pytest.raises(ValueError, match="mystery"):
        render_plugin_view(broken, 60, Styles(None))
