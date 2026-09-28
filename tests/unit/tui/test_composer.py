from __future__ import annotations

from voidcode.tui.composer import Composer, ComposerAction
from voidcode.tui.keys import Key
from voidcode.tui.term import visible_width

from .conftest import plain, theme

WIDTH = 40
MIXED = "你好a😀b world"
NEWLINE = Key("shift+enter")


def composer(width: int = WIDTH, **kwargs: object) -> Composer:
    return Composer(theme=theme(), width=width, **kwargs)  # type: ignore[arg-type]


def typ(c: Composer, text: str) -> None:
    for ch in text:
        c.handle_key(Key(ch, ch))


def paste(c: Composer, text: str) -> None:
    c.handle_key(Key("paste", text))


def submit(c: Composer, text: str) -> None:
    typ(c, text)
    c.handle_key(Key("enter"))


# ---------------------------------------------------------------------------
# Submit vs newline
# ---------------------------------------------------------------------------


def test_enter_submits_and_clears_the_draft() -> None:
    c = composer()
    typ(c, "hello")
    outcome = c.handle_key(Key("enter"))
    assert outcome.action is ComposerAction.SUBMIT
    assert outcome.text == "hello"
    assert c.value == ""


def test_empty_submit_is_ignored() -> None:
    c = composer()
    assert c.handle_key(Key("enter")).action is ComposerAction.NONE


def test_newline_bindings_insert_a_newline() -> None:
    for name in ("shift+enter", "ctrl+j", "ctrl+enter", "alt+enter"):
        c = composer()
        typ(c, "a")
        c.handle_key(Key(name))
        typ(c, "b")
        assert c.value == "a\nb", name
        assert c.handle_key(Key("enter")).action is ComposerAction.SUBMIT


def test_paste_keeps_newlines_and_expands_tabs() -> None:
    c = composer()
    paste(c, "one\r\ntwo\tthree")
    assert c.value == "one\ntwo   three"


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_character_deletion() -> None:
    c = composer()
    paste(c, MIXED)
    c.handle_key(Key("backspace"))
    assert c.value == "你好a😀b worl"
    c = composer()
    paste(c, MIXED)
    c.handle_key(Key("home"))
    c.handle_key(Key("delete"))
    assert c.value == "好a😀b world"
    c = composer()
    paste(c, MIXED)
    c.handle_key(Key("home"))
    c.handle_key(Key("ctrl+d"))
    assert c.value == "好a😀b world"


def test_word_deletion_aliases_agree() -> None:
    for name in ("ctrl+w", "alt+backspace", "ctrl+backspace"):
        c = composer()
        paste(c, MIXED)
        c.handle_key(Key(name))
        assert c.value == "你好a😀b ", name
    for name in ("alt+delete", "alt+d"):
        c = composer()
        paste(c, MIXED)
        c.handle_key(Key("home"))
        c.handle_key(Key(name))
        assert c.value == "a😀b world", name


def test_word_movement_aliases_agree() -> None:
    for name in ("alt+left", "alt+b", "ctrl+left"):
        c = composer()
        paste(c, MIXED)
        c.handle_key(Key(name))
        typ(c, "X")
        assert c.value == "你好a😀b Xworld", name
    for name in ("alt+right", "alt+f", "ctrl+right"):
        c = composer()
        paste(c, MIXED)
        c.handle_key(Key("home"))
        c.handle_key(Key(name))
        typ(c, "X")
        assert c.value == "你好Xa😀b world", name


def test_cursor_movement_within_a_multi_line_draft() -> None:
    up = composer()
    typ(up, "ab")
    up.handle_key(NEWLINE)
    typ(up, "cd")
    up.handle_key(Key("up"))
    typ(up, "X")
    assert up.value == "abX\ncd"

    down = composer()
    typ(down, "ab")
    down.handle_key(NEWLINE)
    typ(down, "cd")
    down.handle_key(Key("up"))
    down.handle_key(Key("down"))
    typ(down, "X")
    assert down.value == "ab\ncdX"


def test_line_boundaries_and_kill_commands() -> None:
    line_start = composer()
    paste(line_start, "ab\ncd")
    line_start.handle_key(Key("home"))
    typ(line_start, "X")
    assert line_start.value == "ab\nXcd"

    line_end = composer()
    paste(line_end, "ab\ncd")
    line_end.handle_key(Key("end"))
    typ(line_end, "X")
    assert line_end.value == "ab\ncdX"

    for start_key in ("home", "ctrl+a"):
        c = composer()
        paste(c, MIXED)
        c.handle_key(Key(start_key))
        c.handle_key(Key("ctrl+k"))
        assert c.value == "", start_key
    for end_key in ("end", "ctrl+e"):
        c = composer()
        paste(c, MIXED)
        c.handle_key(Key(end_key))
        c.handle_key(Key("ctrl+u"))
        assert c.value == "", end_key


def test_left_and_right_move_one_cell() -> None:
    c = composer()
    paste(c, MIXED)
    c.handle_key(Key("home"))
    c.handle_key(Key("right"))
    typ(c, "X")
    assert c.value == "你X好a😀b world"
    c.handle_key(Key("left"))
    c.handle_key(Key("backspace"))
    assert c.value == "X好a😀b world"


# ---------------------------------------------------------------------------
# Wrapping / height
# ---------------------------------------------------------------------------


def test_rows_never_exceed_the_width() -> None:
    corpus = [
        "短文本",
        "a" * 80,
        "emoji 😀😀😀😀😀 mixed in",
        "word " * 30,
        "中文" * 40,
        "line one\nline two\n" + "很长的行" * 20,
        "\n\n\n",
    ]
    for width in (4, 7, 10, 20, 33):
        for text in corpus:
            c = composer(width=width)
            paste(c, text)
            assert c.render()
            for row in c.render():
                assert visible_width(row) <= width, (width, text, row)


def test_render_grows_with_content_and_truncates_at_max_height() -> None:
    c = composer(width=20, max_height=3)
    assert len(c.render()) == 1
    typ(c, "a")
    assert len(c.render()) == 1
    paste(c, "\n\n\n\n\n")
    assert len(c.render()) == 3


def test_placeholder_shows_when_empty_only() -> None:
    c = composer(width=30, placeholder="Ask anything")
    row = plain(list(c.render()))[0]
    # Caret first, placeholder flush right with the minimum gap (omp
    # ``PLACEHOLDER_MIN_GAP``, editor.ts:1312-1319).
    assert visible_width(row) == 30
    assert row.startswith("╰─ ▏")
    assert row.endswith("Ask anything")
    assert "Ask anything" in row
    typ(c, "x")
    assert "Ask anything" not in "".join(plain(list(c.render())))
    assert plain(list(c.render()))[0].startswith("╰─ x▏")


def test_placeholder_hides_when_the_row_cannot_keep_the_gap() -> None:
    # Content width is 27 at width 30; a 25-cell placeholder leaves only the
    # caret's 1 cell, below the 2-cell minimum gap, so it is not drawn.
    c = composer(width=30, placeholder="P" * 25)
    rows = list(c.render())
    assert visible_width(rows[0]) == 30
    assert plain(rows)[0] == "╰─ ▏"
    assert "P" not in plain(rows)[0]


def test_empty_draft_puts_the_caret_at_the_insertion_point() -> None:
    assert plain(list(composer().render()))[0].startswith("╰─ ▏")
    assert plain(list(composer(placeholder="Ask voidcode...").render()))[0].startswith("╰─ ▏")


def test_end_of_line_caret_is_the_input_cursor_glyph_not_nav_cursor() -> None:
    c = composer()
    typ(c, "hello")
    row = plain(list(c.render()))[0]
    assert row.startswith("╰─ hello▏")
    # The list/select glyph must never appear in a composer row (omp's editor
    # reads ``symbols.inputCursor`` only; ``nav.cursor`` is for select widgets).
    assert "❯" not in row
    assert "❯" not in plain(list(composer(placeholder="Ask voidcode...").render()))[0]


def test_ascii_preset_caret_is_a_pipe_and_the_gutter_stays_literal() -> None:
    c = Composer(theme=theme(preset="ascii"), width=WIDTH)
    assert plain(list(c.render()))[0].startswith("╰─ |")
    typ(c, "hello")
    row = plain(list(c.render()))[0]
    assert row.startswith("╰─ hello|")
    # The band gutter is a hard literal (composer/band.ts:17), not a preset glyph.
    assert ">" not in row


def test_mid_text_caret_is_reverse_video_with_no_glyph() -> None:
    c = composer()
    typ(c, "hello")
    c.handle_key(Key("home"))
    row = list(c.render())[0]
    assert "\x1b[7mh\x1b[0m" in row
    assert plain(list(c.render()))[0] == "╰─ hello"
    assert "▏" not in row
    assert "❯" not in row


def test_full_row_caret_underlines_the_last_grapheme_instead_of_overflowing() -> None:
    # Content width 5 at width 8: the 5-cell last visual line has no spare cell
    # for the caret, so its last grapheme is underlined, never pushed past width.
    c = composer(width=8)
    paste(c, "x" * 50)
    rows = list(c.render())
    assert [visible_width(row) for row in rows] == [8] * len(rows)
    last = rows[-1]
    assert "\x1b[4mx\x1b[0m" in last
    assert "▏" not in last
    assert plain(last).endswith("xxxxx")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_recall_dedupes_and_restores_the_draft() -> None:
    c = composer()
    for text in ("one", "one", "two"):
        submit(c, text)
    typ(c, "draft")
    c.handle_key(Key("up"))
    assert c.value == "two"
    c.handle_key(Key("up"))
    assert c.value == "one"
    c.handle_key(Key("up"))
    assert c.value == "one"  # oldest: consecutive duplicate was collapsed
    c.handle_key(Key("down"))
    assert c.value == "two"
    c.handle_key(Key("down"))
    assert c.value == "draft"


def test_history_is_capped_at_one_hundred() -> None:
    c = composer()
    for index in range(105):
        submit(c, str(index))
    c.handle_key(Key("up"))
    assert c.value == "104"
    for _ in range(99):
        c.handle_key(Key("up"))
    assert c.value == "5"
    c.handle_key(Key("up"))
    assert c.value == "5"


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------


def test_tab_is_a_no_op_with_no_completion_surface() -> None:
    # The editor has no completion source: Tab must neither insert text nor
    # open a dropdown, and it must not crash or claim an action.
    c = composer(width=30)
    typ(c, "hello")
    before = list(c.render())
    outcome = c.handle_key(Key("tab"))
    assert outcome.action is ComposerAction.NONE
    assert outcome.text == ""
    assert c.value == "hello"
    assert list(c.render()) == before


def test_escape_cancels_the_turn() -> None:
    c = composer()
    assert c.handle_key(Key("escape")).action is ComposerAction.CANCEL_TURN


# ---------------------------------------------------------------------------
# Ctrl+C / disabled
# ---------------------------------------------------------------------------


def test_ctrl_c_clears_then_double_press_interrupts() -> None:
    clock = [1000.0]
    c = composer(clock=lambda: clock[0])
    typ(c, "draft")
    assert c.handle_key(Key("ctrl+c")).action is ComposerAction.NONE
    assert c.value == ""
    clock[0] += 0.4
    assert c.handle_key(Key("ctrl+c")).action is ComposerAction.INTERRUPT
    clock[0] += 1.0
    typ(c, "again")
    assert c.handle_key(Key("ctrl+c")).action is ComposerAction.NONE
    assert c.value == ""


def test_disabled_composer_ignores_every_key() -> None:
    c = composer()
    c.set_enabled(False)
    typ(c, "abc")
    assert c.value == ""
    assert c.handle_key(Key("enter")).action is ComposerAction.NONE
    assert c.handle_key(Key("ctrl+c")).action is ComposerAction.NONE
    # A disabled composer shows the theme's liveness frame instead of the caret.
    frame = theme().fg("accent", theme().spinner_frames("status")[0])
    assert frame in list(c.render())[0]
