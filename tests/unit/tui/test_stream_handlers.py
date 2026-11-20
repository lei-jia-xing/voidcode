"""Behavioral guards for the split stream event handlers."""

from __future__ import annotations

from typing import Any

from voidcode.sdk.client import CommandCatalog, StreamEvent
from voidcode.sdk.codec import encode_tool_call, encode_tool_result
from voidcode.sdk.provider import ProviderTurnResult
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import CompositeOutput, MediaPart, OutputBounds, TextOutput, ToolCall, ToolSuccess
from voidcode.sdk.transcript import TranscriptEntry, TranscriptTool
from voidcode.tui.stream_handlers import handle_stream_event
from voidcode.tui.terminal_app import CoreTuiApp


class _StubTerminal:
    width = 80
    height = 24
    color_system = None
    eof = False

    def __init__(self) -> None:
        self.frames: list[list[str]] = []
        self.committed: list[str] = []

    def query_default_background_rgb(self) -> None:
        return None

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.chrome_rows))


class _StubClient:
    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog("rev-1", ())

    def health(self) -> None:
        return None


def _app() -> CoreTuiApp:
    return CoreTuiApp(_StubClient(), session_id="s1", terminal=_StubTerminal())


def _event(
    sequence: int,
    event_type: str,
    *,
    session_id: str = "s1",
    content: Any = None,
    **payload: object,
) -> StreamEvent:
    entry = None if content is None else TranscriptEntry(sequence, "run-1", "rev-1", "core", content)
    return StreamEvent(
        session_id=session_id,
        sequence=sequence,
        type=event_type,
        run_id="run-1",
        revision_id="rev-1",
        source="core",
        actor="user",
        payload=dict(payload),
        transcript=entry,
    )


def _transcript_tool() -> TranscriptTool:
    return TranscriptTool(
        ToolCall(tool_name="read", arguments={"path": "a.txt"}),
        ToolSuccess(tool_name="read", output=TextOutput(text="file body")),
        interrupted=False,
        batch_sequence=1,
        deferred=False,
    )


def test_wrong_session_event_never_advances_cursor_or_mutates_state() -> None:
    app = _app()
    rows: list[str] = []

    handle_stream_event(app, _event(5, "core.provider_progress", session_id="other", dispatch_id="d1", text_delta="leak"), rows)

    assert app._session_cursors == {}
    assert app._stream_buffers == {}
    assert rows == []


def test_already_committed_sequence_is_never_rendered_again() -> None:
    app = _app()
    rows: list[str] = []

    handle_stream_event(app, _event(1, "core.tool_completed", content=_transcript_tool()), rows)
    committed_rows = list(rows)
    assert committed_rows

    handle_stream_event(app, _event(1, "core.model_turn", content=ProviderTurnResult(output="replayed")), rows)

    assert app._session_cursors == {"s1": 1}
    assert rows == committed_rows


def test_tool_intent_progress_completed_commits_result_once() -> None:
    app = _app()
    rows: list[str] = []
    call = ToolCall(tool_name="read", arguments={"path": "a.txt"})

    handle_stream_event(
        app,
        _event(1, "core.tool_intent", invocation_id="inv-1", call=encode_tool_call(call), icon={"glyph": "⌁"}),
        rows,
    )
    handle_stream_event(app, _event(2, "core.tool_progress", invocation_id="inv-1", progress={"stream": "stdout", "text": "working\n"}), rows)

    assert rows == []
    assert "inv-1" in app._active_tools

    handle_stream_event(app, _event(3, "core.tool_completed", invocation_id="inv-1", content=_transcript_tool()), rows)

    assert app._active_tools == {}
    assert sum("file body" in row for row in rows) == 1
    assert sum("read" in row for row in rows) == 1


def test_raw_tool_completed_decodes_wire_result_for_artifact_sources() -> None:
    reference = ArtifactReference("e" * 64, 21, "application/json")
    result = ToolSuccess("read", TextOutput(text="body"), result_reference=reference)
    app = _app()
    rows: list[str] = []

    handle_stream_event(app, _event(1, "core.tool_completed", invocation_id="inv-1", result=encode_tool_result(result)), rows)

    assert rows == []
    assert app._latest_tool_artifacts is not None
    tool_name, sources = app._latest_tool_artifacts
    assert tool_name == "read"
    assert [source.sha256 for source in sources] == [reference.sha256]


def test_typed_tool_artifacts_are_read_directly_without_encoding(monkeypatch: Any) -> None:
    import voidcode.sdk.codec as codec
    import voidcode.tui.terminal_app as terminal_app

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("typed tool artifacts must not be encoded back to wire JSON")

    monkeypatch.setattr(codec, "encode_tool_result", forbidden)
    monkeypatch.setattr(terminal_app, "encode_tool_result", forbidden, raising=False)
    bounds_reference = ArtifactReference("b" * 64, 12, "text/plain")
    part_reference = ArtifactReference("c" * 64, 34, "image/png")
    result_reference = ArtifactReference("d" * 64, 56, "application/json")
    result = ToolSuccess(
        "capture",
        CompositeOutput(
            parts=(MediaPart(reference=part_reference, mime="image/png", presentation="chart"),),
            bounds=OutputBounds(reference=bounds_reference),
        ),
        result_reference=result_reference,
    )
    app = _app()

    app._record_tool_artifact(TranscriptTool(ToolCall("capture", {}), result, False, 1, False))

    assert app._latest_tool_artifacts is not None
    tool_name, sources = app._latest_tool_artifacts
    assert tool_name == "capture"
    assert [source.label for source in sources] == [
        "Original output source",
        "Part 0 (media)",
        "Complete structured result JSON",
    ]
    assert [source.sha256 for source in sources] == [
        bounds_reference.sha256,
        part_reference.sha256,
        result_reference.sha256,
    ]
