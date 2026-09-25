from __future__ import annotations

import re

from voidcode.tui.term import visible_width
from voidcode.tui.transcript import (
    PREVIEW_LIMITS,
    AssistantBlock,
    DiffBlock,
    ErrorBlock,
    KeyHints,
    NoticeBlock,
    SessionMarkerBlock,
    ThinkingBlock,
    ToolBlock,
    Transcript,
    UserBlock,
    format_code_frame_line,
    format_key_hint,
)

from .conftest import plain, theme

WIDTH = 60
WIDE_DIFF = """@@ -96,3 +96,6 @@
 context ninety-six
-removed ninety-seven
+added ninety-seven
+added ninety-eight
 context ninety-nine
"""


def tape(width: int = WIDTH, **kwargs: object) -> Transcript:
    return Transcript(theme(), width, **kwargs)  # type: ignore[arg-type]


def rows_of(block: object, *, width: int = WIDTH, preset: str = "unicode", hints: KeyHints | None = None) -> list[str]:
    """Render one block through the tape (the consumer-visible path)."""
    transcript = Transcript(theme(preset=preset), width, hints=hints or KeyHints())
    transcript.add(block)  # type: ignore[arg-type]
    return transcript.rows()


# ---------------------------------------------------------------------------
# Tape shape
# ---------------------------------------------------------------------------


def test_blocks_are_separated_by_exactly_one_blank_row() -> None:
    transcript = tape()
    transcript.add(NoticeBlock(text="first"))
    transcript.add(NoticeBlock(text="second"))
    transcript.add(NoticeBlock(text="third"))
    assert plain(transcript.rows()) == [" first", "", " second", "", " third"]


def test_blank_edges_are_trimmed_so_separators_never_double() -> None:
    transcript = tape()
    transcript.add(NoticeBlock(text=""))
    transcript.add(NoticeBlock(text="body"))
    assert plain(transcript.rows()) == [" body"]


def test_rows_never_exceed_the_render_width() -> None:
    transcript = tape(width=24)
    transcript.add(UserBlock(text="你好世界 " * 6))
    transcript.add(NoticeBlock(text="🎉" * 40))
    transcript.add(AssistantBlock(text="a very long word " * 8))
    transcript.add(ToolBlock(tool="bash", title="Bash", summary="x" * 90, body=["🎉" * 60], expanded=True))
    for row in transcript.rows():
        assert visible_width(row) <= 24


def test_user_block_is_a_full_width_background_band() -> None:
    transcript = tape(width=40)
    transcript.add(UserBlock(text="hello"))
    rows = transcript.rows()
    assert all(visible_width(row) == 40 for row in rows)
    assert all(row.startswith("\x1b[48;2;15;18;22m") for row in rows)
    # The band covers every cell, so the trailing padding is part of the row.
    assert re.sub(r"\x1b\[[0-9;]*m", "", rows[0]) == " hello" + " " * 34


def test_settled_and_live_halves_split_at_the_frontier() -> None:
    transcript = tape()
    transcript.add(NoticeBlock(text="settled one"))
    transcript.add(NoticeBlock(text="settled two"))
    streaming = transcript.add(AssistantBlock(text="streaming", settled=False))
    assert transcript.frontier() == 2
    assert plain(transcript.settled_rows()) == [" settled one", "", " settled two"]
    assert plain(transcript.live_rows()) == [" streaming"]

    streaming.settled = True
    assert transcript.frontier() == 3
    assert transcript.live_rows() == []
    assert plain(transcript.rows())[-1] == " streaming"


def test_take_settled_hands_out_each_row_once() -> None:
    transcript = tape()
    transcript.add(NoticeBlock(text="one"))
    assert transcript.take_settled() == transcript.settled_rows()
    assert transcript.take_settled() == []
    transcript.add(NoticeBlock(text="two"))
    assert [row.strip() for row in plain(transcript.take_settled())] == ["", "two"]


def test_mark_settled_stops_streaming_and_the_pulse_row() -> None:
    transcript = tape()
    assistant = transcript.add(AssistantBlock(text="hi", streaming=True, pulse_frame=4, settled=False))
    tool = transcript.add(ToolBlock(tool="edit", title="Edit", streaming=True, spinner_frame=2, settled=False))
    transcript.mark_settled()
    assert assistant.pulse_frame is None
    assert tool.streaming is False
    assert not any("streaming" in row for row in plain(transcript.rows()))


def test_rewind_re_hands_out_the_settled_tape() -> None:
    """A change inside the committed prefix needs the whole tape handed out again."""
    transcript = tape()
    transcript.add(NoticeBlock(text="one"))
    handed_out = transcript.take_settled()
    assert transcript.take_settled() == []

    transcript.rewind()
    # Expanding an already-committed block rewrote its rows in place; the caller
    # re-prints, because native scrollback cannot be rewritten.
    transcript.set_expanded(True)
    again = transcript.take_settled()
    assert again == handed_out
    assert transcript.take_settled() == []


# ---------------------------------------------------------------------------
# Key hints
# ---------------------------------------------------------------------------


def test_key_hint_formatting_port() -> None:
    assert format_key_hint("ctrl+o") == "Ctrl+O"
    assert format_key_hint("alt+e", "darwin") == "Option+E"
    assert format_key_hint("alt+e", "linux") == "Alt+E"
    assert format_key_hint("escape") == "Esc"
    assert format_key_hint("f5") == "F5"


def test_expand_hint_is_generated_from_the_binding() -> None:
    hints = KeyHints(expand="alt+e")
    block = ThinkingBlock(text="line one\nline two", expanded=False)
    collapsed = plain(rows_of(block, hints=hints))[0].strip()
    assert collapsed == "◑ med Thinking · 2 lines ⟦Alt+E: Expand⟧"

    block.expanded = True
    assert not any("Expand" in row for row in plain(rows_of(block, hints=hints)))


def test_expand_hint_uses_the_glyph_preset_brackets() -> None:
    block = ThinkingBlock(text="x")
    assert "⟦Ctrl+O: Expand⟧" in plain(rows_of(block))[0]
    assert "[Ctrl+O: Expand]" in plain(rows_of(block, preset="ascii"))[0]


def test_more_lines_hint_matches_the_count() -> None:
    block = ToolBlock(tool="bash", title="Bash", body=[f"line-{index}" for index in range(9)], state="success")
    rows = [row.strip() for row in plain(rows_of(block))]
    assert "… 6 more lines (ctrl+o to expand)" in rows
    assert f"line-{PREVIEW_LIMITS['OUTPUT_COLLAPSED'] - 1}" in rows
    assert f"line-{PREVIEW_LIMITS['OUTPUT_COLLAPSED']}" not in rows


# ---------------------------------------------------------------------------
# Tool cards
# ---------------------------------------------------------------------------


def test_tool_header_is_icon_title_and_dotted_summary() -> None:
    settled = plain(rows_of(ToolBlock(tool="read", title="Read", summary="src/app.py", state="success")))[0]
    running = plain(rows_of(ToolBlock(tool="read", title="Read", summary="src/app.py", state="running")))[0]
    assert settled == "✔ Read · src/app.py"
    assert running == "📄 Read · src/app.py"


def test_expanded_tool_card_is_framed_and_collapsed_is_not() -> None:
    collapsed = plain(rows_of(ToolBlock(tool="bash", title="Bash", body=["out"], state="success"), width=40))
    expanded_rows = rows_of(ToolBlock(tool="bash", title="Bash", body=["out"], state="success", expanded=True), width=40)
    bordered = plain(expanded_rows)
    assert not collapsed[0].startswith("╭")
    assert bordered[0].startswith("╭─── ") and bordered[0].endswith("╮") and "Bash" in bordered[0]
    assert bordered[-1].startswith("╰") and bordered[-1].endswith("╯")
    assert all(visible_width(row) == 40 for row in expanded_rows)


def test_streaming_card_keeps_the_animated_glyph_off_the_header_row() -> None:
    block = ToolBlock(tool="edit", title="Edit", body=["diff"], expanded=True, streaming=True, spinner_frame=1)
    rows = plain(rows_of(block, width=44))
    header = rows[0]
    assert not any(glyph in header for glyph in theme().spinner_frames("status"))
    assert header.startswith("╭─── Edit")
    assert rows[-1].strip() == "⣽ … (streaming)"


# ---------------------------------------------------------------------------
# Diff gutter
# ---------------------------------------------------------------------------


def test_code_frame_gutter_is_a_fixed_three_digit_field() -> None:
    assert format_code_frame_line("+", 7, "content", 3) == "  +7│content"
    assert format_code_frame_line("-", 123, "content", 3) == "-123│content"
    assert format_code_frame_line(" ", 123, "content", 3) == " 123│content"
    assert format_code_frame_line("+", "", "content", 3) == "   +│content"


def test_diff_rows_are_byte_identical_between_streaming_and_settled() -> None:
    lines = WIDE_DIFF.splitlines()
    streaming = DiffBlock(diff="\n".join(lines[:4]), expanded=True)
    settled = DiffBlock(diff=WIDE_DIFF, expanded=True)
    streaming_rows = rows_of(streaming, width=80)
    settled_rows = rows_of(settled, width=80)
    assert streaming_rows == settled_rows[: len(streaming_rows)]


def test_diff_gutter_does_not_widen_past_three_digits() -> None:
    block = DiffBlock(
        diff="\n".join(["@@ -98,3 +98,3 @@", " a", " b", " c"]) + "\n-removed\n+added\n",
        expanded=True,
    )
    rows = plain(rows_of(block, width=80))
    gutter_rows = [row for row in rows if "│" in row]
    assert gutter_rows
    # 3-digit minimum plus the marker column: the gutter never widens, even when
    # the line numbers cross 100.
    assert {len(row.split("│")[0]) for row in gutter_rows} == {4}
    assert any(row.split("│")[0].endswith("100") for row in gutter_rows)


def test_diff_rows_render_line_numbers_markers_and_indent_visualization() -> None:
    diff = "@@ -1,2 +1,2 @@\n context line\n-\tremoved\n+  added\n"
    rows = plain(rows_of(DiffBlock(diff=diff, expanded=True), width=80))
    assert rows[1] == "   1│context line"
    assert rows[2] == "  -2│  → removed"
    # A replacement repeats the line number: omp blanks the duplicate gutter
    # (chrome/diff.ts:139-147), leaving the marker alone in the field.
    assert rows[3] == "   +│··added"


def test_collapsed_diff_reports_hidden_lines() -> None:
    body = "\n".join(["@@ -1,60 +1,60 @@"] + [f" context {index}" for index in range(60)])
    rows = plain(rows_of(DiffBlock(diff=body), width=80))
    assert len(rows) == PREVIEW_LIMITS["DIFF_COLLAPSED_LINES"]
    assert rows[-1].strip().endswith(f"({KeyHints().expand} to expand)")


# ---------------------------------------------------------------------------
# Other blocks
# ---------------------------------------------------------------------------


def test_error_and_notice_blocks_carry_state_glyphs() -> None:
    error = plain(rows_of(ErrorBlock(text="boom")))
    notice = plain(rows_of(NoticeBlock(text="fyi")))
    assert error[0].startswith(" ✘ boom")
    assert notice[0] == " fyi"


def test_session_marker_draws_a_labelled_rule() -> None:
    rows = plain(rows_of(SessionMarkerBlock(label="session 4f2a", hint="ctrl+o"), width=40))
    assert len(rows) == 1
    assert visible_width(rows[0]) == 40
    assert "session 4f2a" in rows[0] and "ctrl+o" in rows[0]
    assert rows[0].startswith("─")


def test_narrow_session_marker_falls_back_to_the_bare_label() -> None:
    rows = plain(rows_of(SessionMarkerBlock(label="session four", hint="ctrl+o"), width=8))
    assert rows == ["session four"]
