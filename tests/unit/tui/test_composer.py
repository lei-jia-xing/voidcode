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


def completer(query: str) -> list[str]:
    return [candidate for candidate in ("/expand", "/exit") if candidate.startswith(query)]


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
    assert "Ask anything" in plain(list(c.render()))[0]
    typ(c, "x")
    assert "Ask anything" not in "".join(plain(list(c.render())))


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
# Completion
# ---------------------------------------------------------------------------


def test_completion_opens_filters_selects_accepts_and_cancels() -> None:
    c = composer(width=30, completer=completer)
    typ(c, "/e")
    base_rows = len(c.render())
    c.handle_key(Key("tab"))
    assert len(c.render()) == base_rows + 2
    typ(c, "xi")  # "/exi" filters out /expand
    assert len(c.render()) == base_rows + 1
    c.handle_key(Key("up"))
    c.handle_key(Key("enter"))
    assert c.value == "/exit"

    c = composer(width=30, completer=completer)
    typ(c, "/e")
    c.handle_key(Key("tab"))
    c.handle_key(Key("down"))
    c.handle_key(Key("tab"))
    assert c.value == "/exit"

    c = composer(width=30, completer=completer)
    typ(c, "/e")
    c.handle_key(Key("tab"))
    outcome = c.handle_key(Key("escape"))
    assert outcome.action is ComposerAction.NONE
    assert len(c.render()) == base_rows


def test_tab_completes_the_expand_command_then_enter_submits() -> None:
    c = composer(width=30, completer=lambda query: ["/expand"] if "/expand".startswith(query) else [])
    typ(c, "/ex")
    c.handle_key(Key("tab"))
    c.handle_key(Key("tab"))
    assert c.value == "/expand"
    outcome = c.handle_key(Key("enter"))
    assert outcome.action is ComposerAction.SUBMIT
    assert outcome.text == "/expand"


def test_escape_cancels_the_turn_without_completion() -> None:
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
    assert c.tick() is True


def test_enabled_tick_is_a_no_op() -> None:
    assert composer().tick() is False
