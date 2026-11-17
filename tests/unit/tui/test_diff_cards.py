"""Semantic diff cards consume single-file edit bodies; tables/JSON stay readable."""

from __future__ import annotations

import re

from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import OpaqueToolBody, TextOutput, ToolCall, ToolSuccess
from voidcode.sdk.transcript import TranscriptTool
from voidcode.tui.artifact_viewer import artifact_sources
from voidcode.tui.styles import Styles
from voidcode.tui.terminal_app import CoreTuiApp
from voidcode.tui.views import render_tool_result

PLAIN = re.compile(r"\x1b\[[0-9;]*m")

_DIFF = "--- src/foo.py\n+++ src/foo.py\n@@ -1,3 +1,3 @@\n line1\n-const a = 1;\n+const a = 2;\n line3\n"


def _edit_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "path": "src/foo.py",
        "additions": 1,
        "deletions": 1,
        "match_count": 1,
        "diff": _DIFF,
        "content_hash": "f" * 64,
    }
    body.update(overrides)
    return body


def _edit_result(body: dict[str, object] | None, **extra: object) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "ok",
        "tool_name": "edit",
        "output": {"type": "text", "text": "Edit applied successfully."},
        "body": body,
    }
    result.update(extra)
    return result


def test_edit_diff_card_shows_file_stats_and_hunks() -> None:
    text = "\n".join(render_tool_result(_edit_result(_edit_body()), 100, Styles(None)))
    assert "Edit applied successfully." in text
    assert "Diff: src/foo.py (+1 -1)" in text
    assert "@@ -1,3 +1,3 @@" in text
    assert "-const a = 1;" in text
    assert "+const a = 2;" in text
    assert text.index("Edit applied successfully.") < text.index("Diff: src/foo.py")


def test_diff_card_keeps_output_and_artifact_entry_in_order() -> None:
    reference = {"sha256": "c" * 64, "bytes": 42, "media_type": "application/json"}
    text = "\n".join(render_tool_result(_edit_result(_edit_body(), result_reference=reference), 100, Styles(None)))
    assert "Edit applied successfully." in text
    assert "Diff: src/foo.py (+1 -1)" in text
    assert "Artifacts: Ctrl+O / /tui output" in text
    assert text.index("Diff: src/foo.py") < text.index("Artifacts: Ctrl+O")
    sources = artifact_sources(_edit_result(_edit_body(), result_reference=reference))
    assert [source.label for source in sources] == ["Complete structured result JSON"]
    assert sources[0].sha256 == "c" * 64


def test_diff_card_renders_without_inline_output_text() -> None:
    result: dict[str, object] = {
        "status": "ok",
        "tool_name": "edit",
        "output": {"type": "empty", "presentation": ""},
        "body": _edit_body(),
    }
    text = "\n".join(render_tool_result(result, 100, Styles(None)))
    assert "Diff: src/foo.py (+1 -1)" in text
    assert "+const a = 2;" in text


def test_diff_body_is_ignored_on_failure() -> None:
    result = _edit_result(_edit_body(), status="error", error="edit failed")
    text = "\n".join(render_tool_result(result, 100, Styles(None)))
    assert "Error: edit failed" in text
    assert "Diff: src/foo.py" not in text


def test_partial_diff_shapes_preserve_output_only() -> None:
    cases: list[dict[str, object] | None] = [
        None,
        {},
        _edit_body(path=""),
        _edit_body(path=123),
        _edit_body(diff=123),
        _edit_body(diff=""),
        _edit_body(additions=True),
        _edit_body(additions=-1),
        _edit_body(additions="1"),
        _edit_body(deletions=False),
        _edit_body(deletions=-2),
        _edit_body(deletions=None),
    ]
    for body in cases:
        text = "\n".join(render_tool_result(_edit_result(body), 100, Styles(None)))
        assert "Edit applied successfully." in text
        assert "Diff:" not in text


def test_diff_controls_are_escaped_never_executed() -> None:
    body = _edit_body(path="a\x1b[2J.py", diff="@@ x @@\n+hi\x07there\x1b[31m\n")
    text = "\n".join(render_tool_result(_edit_result(body), 120, Styles(None)))
    assert "\x1b" not in text and "\x07" not in text
    assert "a\\x1b[2J.py" in text
    assert "hi\\x07there\\x1b[31m" in text


def test_diff_dispatch_ignores_tool_name() -> None:
    for tool_name in ("edit", "custom_tool", "mystery", ""):
        result = _edit_result(_edit_body())
        result["tool_name"] = tool_name
        text = "\n".join(render_tool_result(result, 100, Styles(None)))
        assert "Diff: src/foo.py (+1 -1)" in text
    # A bare edit output without a diff body never invents a card from the name.
    bare: dict[str, object] = {
        "status": "ok",
        "tool_name": "edit",
        "output": {"type": "text", "text": "Edit applied successfully."},
        "body": {"path": "src/foo.py"},
    }
    assert "Diff:" not in "\n".join(render_tool_result(bare, 100, Styles(None)))


def test_large_diff_preserves_every_hunk_and_resource_identity() -> None:
    hunks = "".join(f"@@ -{i},1 +{i},1 @@\n-old {i}\n+new {i}\n" for i in range(1, 51))
    diff = f"--- big.py\n+++ big.py\n{hunks}"
    reference = {"sha256": "d" * 64, "bytes": 999, "media_type": "application/json"}
    body = _edit_body(path="big.py", additions=50, deletions=50, diff=diff)
    result = _edit_result(body, result_reference=reference)
    text = "\n".join(render_tool_result(result, 120, Styles(None)))
    assert "Diff: big.py (+50 -50)" in text
    assert "-old 1" in text and "+new 50" in text
    assert "@@ -25,1 +25,1 @@" in text
    assert "Artifacts: Ctrl+O / /tui output" in text
    sources = artifact_sources(result)
    assert sources and sources[0].sha256 == "d" * 64


def test_composite_table_json_keep_hierarchy_and_references() -> None:
    table_ref = {"sha256": "a" * 64, "bytes": 90, "media_type": "application/json"}
    json_ref = {"sha256": "b" * 64, "bytes": 12, "media_type": "application/json"}
    result: dict[str, object] = {
        "status": "ok",
        "output": {
            "type": "composite",
            "parts": [
                {
                    "kind": "table",
                    "columns": ["id", "name"],
                    "rows": [["1", "ada"], ["2", "bob"]],
                    "total_rows": 7,
                    "reference": table_ref,
                    "presentation": "Top rows",
                },
                {"kind": "json", "reference": json_ref, "preview": '{"ok": true}'},
            ],
            "bounds": {},
        },
    }
    text = "\n".join(render_tool_result(result, 120, Styles(None)))
    assert "Top rows" in text and "ada" in text and "bob" in text
    assert "[showing 2/7 rows; full table: artifact://" + "a" * 64 + "]" in text
    assert '{"ok": true}' in text
    assert "[full JSON: artifact://" + "b" * 64 + "]" in text
    sources = artifact_sources(result)
    assert [source.sha256 for source in sources] == ["a" * 64, "b" * 64]


def test_media_fallback_stays_readable_without_images() -> None:
    result: dict[str, object] = {
        "status": "ok",
        "output": {
            "type": "composite",
            "parts": [
                {
                    "kind": "media",
                    "reference": {"sha256": "e" * 64, "bytes": 128, "media_type": "image/png"},
                    "mime": "image/png",
                    "presentation": "chart",
                },
            ],
            "bounds": {},
        },
    }
    text = "\n".join(render_tool_result(result, 200, Styles(None)))
    assert "[Image: image/png, 128 bytes, artifact://" + "e" * 64 + "] chart" in text


def _harness_app() -> CoreTuiApp:
    from voidcode.sdk.client import CommandCatalog

    class _StubTerminal:
        width = 100
        eof = False
        color_system = None

        def __init__(self) -> None:
            self.committed: list[str] = []

        def commit_rows(self, rows: list[str]) -> None:
            self.committed.extend(rows)

        def paint_frame(self, frame: object) -> None:
            return None

    class _StubClient:
        def command_catalog(self) -> CommandCatalog:
            return CommandCatalog(revision_id="rev-1", commands=())

    app = CoreTuiApp(_StubClient(), session_id="s1", terminal=_StubTerminal())  # type: ignore[arg-type]
    return app


def test_edit_card_flows_through_transcript_harness() -> None:
    from voidcode.sdk.client import StreamEvent
    from voidcode.sdk.transcript import TranscriptEntry

    app = _harness_app()
    body = OpaqueToolBody(
        {
            "path": "src/foo.py",
            "additions": 1,
            "deletions": 1,
            "match_count": 1,
            "diff": _DIFF,
            "content_hash": "f" * 64,
        }
    )
    tool = TranscriptTool(
        ToolCall(tool_name="edit", arguments={"path": "src/foo.py"}, tool_call_id="call-1"),
        ToolSuccess(tool_name="edit", output=TextOutput(text="Edit applied successfully."), body=body),
        interrupted=False,
        batch_sequence=1,
        deferred=False,
    )
    event = StreamEvent(
        session_id="s1",
        sequence=6,
        type="core.tool_completed",
        run_id="r",
        revision_id="rev-1",
        source="k",
        actor="m",
        payload={"invocation_id": "iv-1", "batch_sequence": 1},
        transcript=TranscriptEntry(6, "r", "rev-1", "k", tool),
    )
    rows: list[str] = []
    app._handle_stream_event(event, rows)
    text = "\n".join(PLAIN.sub("", row) for row in rows)
    assert "Edit applied successfully." in text
    assert "Diff: src/foo.py (+1 -1)" in text
    assert "-const a = 1;" in text and "+const a = 2;" in text


def test_typed_table_view_reference_round_trip() -> None:
    from voidcode.sdk.views import TableViewData
    from voidcode.tui.views import render_table

    data = TableViewData(
        columns=("id",),
        rows=(("1",),),
        total_rows=9,
        reference=ArtifactReference("c" * 64, 90, "application/json"),
    )
    text = "\n".join(render_table(data, 80, Styles(None)))
    assert "[showing 1/9 rows; full table: artifact://" + "c" * 64 + "]" in text
