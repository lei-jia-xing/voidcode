"""Tool-declared icon consumption (no name mapping in TUI)."""

from __future__ import annotations

import re

from rich.color import ColorSystem

from voidcode.sdk.icons import Icon
from voidcode.tui.stream_handlers import handle_stream_event
from voidcode.tui.styles import Styles, render_icon
from voidcode.tui.term import visible_width
from voidcode.tui.views import render_tool_call

PLAIN = re.compile(r"\x1b\[[0-9;]*m")

_WIDE = "\u754c"  # 2-cell CJK glyph: a real width boundary
_CLUSTER = "\U0001f469\u200d\U0001f4bb"  # ZWJ cluster: one grapheme, must not split


def _plain(rows: list[str]) -> list[str]:
    return [PLAIN.sub("", row) for row in rows]


def test_renders_declared_glyph() -> None:
    icon = Icon(glyph="\u276f")
    assert render_icon(icon) == "\u276f"
    # Distinct meanings, not pinned pixels: None -> a generic mark, empty -> nothing.
    assert render_icon(None) != ""
    assert render_icon(Icon(glyph="")) == ""


def test_builtin_looking_name_with_custom_icon_is_not_guessed() -> None:
    """A tool named like a builtin still renders exactly its declared icon."""
    icon = Icon(glyph="\u754c")
    rows = render_tool_call("read", {}, 60, Styles(ColorSystem.STANDARD), icon=icon)
    assert "\u754c" in _plain(rows)[0]


def test_icon_control_bytes_and_markup_are_visible_never_executed() -> None:
    icon = Icon(glyph="x\x1b[31m")
    assert render_icon(icon) == "x\\x1b[31m"
    rows = render_tool_call("tool", {}, 60, Styles(ColorSystem.STANDARD), icon=icon)
    joined = "".join(rows)
    assert "\x1b[31m" not in joined
    assert "x\\x1b[31m" in PLAIN.sub("", joined)
    markup = render_tool_call("tool", {}, 60, Styles(ColorSystem.STANDARD), icon=Icon(glyph="[red]x[/red]"))
    assert "[red]x[/red]" in _plain(markup)[0]  # plain Text, never Rich markup


def test_wide_glyph_and_zwj_cluster_stay_whole_at_narrow_width() -> None:
    styles = Styles(ColorSystem.STANDARD)
    for mark in (_WIDE, _CLUSTER):
        rows = render_tool_call("tool", {}, 16, styles, icon=Icon(glyph=mark))
        plain = _plain(rows)
        assert mark in plain[0], f"{mark!r} split or dropped at width 16"
        assert all(visible_width(row) <= 16 for row in plain)


def _icon_app():
    """App + rows with the shared stub client/terminal."""
    from voidcode.core.events import EventType  # noqa: F401
    from voidcode.sdk.client import CommandCatalog
    from voidcode.tui.terminal_app import CoreTuiApp

    class _StubTerminal:
        width = 100
        height = 24
        eof = False
        color_system = None

        def __init__(self) -> None:
            self.committed: list[str] = []
            self.frames: list[list[str]] = []

        def commit_rows(self, rows: list[str]) -> None:
            self.committed.extend(rows)

        def paint_frame(self, frame: object) -> None:
            self.frames.append(list(frame.rows))  # type: ignore[attr-defined]

    class _StubClient:
        def command_catalog(self) -> CommandCatalog:
            return CommandCatalog(revision_id="rev-1", commands=())

    return CoreTuiApp(_StubClient(), session_id="s1", terminal=_StubTerminal())  # type: ignore[arg-type]


def _stream_event(sequence: int, event_type: str, payload: dict, content=None):
    from voidcode.sdk.client import StreamEvent
    from voidcode.sdk.transcript import TranscriptEntry

    entry = TranscriptEntry(sequence, "r", "rev-1", "k", content) if content is not None else None
    return StreamEvent(
        session_id="s1",
        sequence=sequence,
        type=event_type,
        run_id="r",
        revision_id="rev-1",
        source="k",
        actor="m",
        payload=payload,
        transcript=entry,
    )


def test_intent_header_then_result_renders_once_with_owned_icon() -> None:
    """Live intent commits the owned header; the settled result adds no second header."""
    from voidcode.sdk.tools import TextOutput, ToolCall, ToolSuccess
    from voidcode.sdk.transcript import TranscriptTool

    app = _icon_app()
    rows: list[str] = []
    handle_stream_event(
        app,
        _stream_event(
            5,
            "core.tool_intent",
            {
                "invocation_id": "iv-1",
                "batch_sequence": 4,
                "call": {"tool_name": "read", "arguments": {}, "tool_call_id": "call-9"},
                "icon": {"glyph": "\u2211"},
            },
        ),
        rows,
    )
    handle_stream_event(
        app,
        _stream_event(
            6,
            "core.tool_completed",
            {"invocation_id": "iv-1", "batch_sequence": 4},
            TranscriptTool(
                ToolCall(tool_name="read", arguments={}, tool_call_id="call-9"),
                ToolSuccess(tool_name="read", output=TextOutput(text="body")),
                interrupted=False,
                batch_sequence=4,
                deferred=False,
            ),
        ),
        rows,
    )
    plain = _plain(rows)
    assert sum("read" in row and "\u2211" in row for row in plain) == 1
    assert any("body" in row for row in plain)


def test_same_call_id_across_batches_never_leaks_icon() -> None:
    """Invocation ids key headers: a repeated call id in a later batch gets its own icon."""
    from voidcode.sdk.tools import TextOutput, ToolCall, ToolSuccess
    from voidcode.sdk.transcript import TranscriptTool

    app = _icon_app()
    rows: list[str] = []
    for seq, invocation, glyph in ((5, "iv-1", "\u2211"), (9, "iv-2", "\u754c")):
        handle_stream_event(
            app,
            _stream_event(
                seq,
                "core.tool_intent",
                {
                    "invocation_id": invocation,
                    "batch_sequence": seq - 1,
                    "call": {"tool_name": "read", "arguments": {}, "tool_call_id": "call-9"},
                    "icon": {"glyph": glyph},
                },
            ),
            rows,
        )
        handle_stream_event(
            app,
            _stream_event(
                seq + 1,
                "core.tool_completed",
                {"invocation_id": invocation, "batch_sequence": seq - 1},
                TranscriptTool(
                    ToolCall(tool_name="read", arguments={}, tool_call_id="call-9"),
                    ToolSuccess(tool_name="read", output=TextOutput(text=f"body-{invocation}")),
                    interrupted=False,
                    batch_sequence=seq - 1,
                    deferred=False,
                ),
            ),
            rows,
        )
    plain = _plain(rows)
    assert any("\u2211" in row for row in plain) and any("\u754c" in row for row in plain)
    assert sum("\u2211" in row for row in plain) == 1 and sum("\u754c" in row for row in plain) == 1


def test_settled_result_without_intent_emits_recorded_icon_header_first() -> None:
    """Typed history (no live intent): the recorded icon heads the result, failures included."""
    import pytest

    from voidcode.sdk.icons import Icon
    from voidcode.sdk.tools import TextOutput, ToolCall, ToolFailure, ToolSuccess
    from voidcode.sdk.transcript import TranscriptTool

    app = _icon_app()
    rows: list[str] = []
    handle_stream_event(
        app,
        _stream_event(
            6,
            "core.tool_completed",
            {"batch_sequence": 4},
            TranscriptTool(
                ToolCall(tool_name="read", arguments={}),
                ToolSuccess(tool_name="read", output=TextOutput(text="body")),
                interrupted=False,
                batch_sequence=4,
                deferred=False,
                icon=Icon(glyph="\u2211"),
            ),
        ),
        rows,
    )
    plain = _plain(rows)
    header_at = next(i for i, row in enumerate(plain) if "\u2211" in row and "read" in row)
    body_at = next(i for i, row in enumerate(plain) if "body" in row)
    assert header_at < body_at

    failing: list[str] = []
    handle_stream_event(
        app,
        _stream_event(
            7,
            "core.tool_interrupted",
            {"batch_sequence": 4},
            TranscriptTool(
                ToolCall(tool_name="exec", arguments={}),
                ToolFailure(tool_name="exec", error="stop", output=TextOutput(text="raw")),
                interrupted=True,
                batch_sequence=4,
                deferred=False,
                icon=Icon(glyph="\u754c"),
            ),
        ),
        failing,
    )
    failing_plain = _plain(failing)
    header_at = next(i for i, row in enumerate(failing_plain) if "\u754c" in row)
    body_at = next(i for i, row in enumerate(failing_plain) if "stop" in row)
    assert header_at < body_at

    with pytest.raises(ValueError, match="glyph string"):
        handle_stream_event(
            app,
            _stream_event(8, "core.tool_intent", {"invocation_id": "iv-x", "call": {"tool_name": "t", "arguments": {}}, "icon": {"glyph": 1}}),
            [],
        )
