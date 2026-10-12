from voidcode.runtime.config import RuntimeTuiStatuslineConfig
from voidcode.tui.composer import Composer
from voidcode.tui.keys import Key, KeyDecoder
from voidcode.tui.statusline import StatusLine, StatusSegmentData
from voidcode.tui.term import visible_width
from voidcode.tui.theme import resolve_theme


def test_paste_preserves_whitespace_and_edits_whole_graphemes() -> None:
    theme = resolve_theme(color_system=None)
    composer = Composer(theme=theme, width=8, max_height=4)
    pasted = "  \te\u0301👩\u200d💻  "

    decoder = KeyDecoder()
    tab_key = decoder.feed(b"\t")
    assert tab_key == [Key("tab")]
    composer.handle_key(tab_key[0])
    assert composer.value == ""
    paste_key = decoder.feed(b"\x1b[200~" + pasted.encode() + b"\x1b[201~")
    assert paste_key == [Key("paste", pasted)]
    composer.handle_key(paste_key[0])
    assert composer.value == pasted

    rows = composer.render()
    assert all("\t" not in row and visible_width(row) <= 8 for row in rows)

    composer.handle_key(Key("left"))
    composer.handle_key(Key("backspace"))
    assert composer.value == pasted[:-1]
    composer.handle_key(Key("delete"))
    assert composer.value == "  \te\u0301👩\u200d💻"
    composer.handle_key(Key("left"))
    composer.handle_key(Key("right"))
    composer.handle_key(Key("backspace"))
    assert composer.value == "  \te\u0301"
    composer.handle_key(Key("backspace"))
    assert composer.value == "  \t"


def test_paste_normalizes_crlf_and_cr_to_editor_newlines() -> None:
    composer = Composer(theme=resolve_theme(color_system=None), width=20)
    pasted = "  café\tfirst\r\nsecond\rthird  "

    paste_key = KeyDecoder().feed(b"\x1b[200~" + pasted.encode() + b"\x1b[201~")
    assert paste_key == [Key("paste", pasted)]
    composer.handle_key(paste_key[0])

    assert composer.value == "  café\tfirst\nsecond\nthird  "


def test_ten_line_paste_renders_and_remains_editable_in_short_composer() -> None:
    composer = Composer(
        theme=resolve_theme(color_system=None),
        width=20,
        max_height=2,
        style="band",
        gutter="none",
        padding="compact",
    )
    pasted = "\n".join(f"line-{index:02d}" for index in range(10))
    composer.handle_key(Key("paste", pasted))

    rows = composer.render()
    assert composer.value == pasted
    cursor = composer.cursor_position
    assert cursor is not None
    row, column = cursor
    assert 0 <= row < len(rows) and 0 <= column < 20
    assert "line-09" in rows[row]

    composer.handle_key(Key("left"))
    composer.handle_key(Key("backspace"))
    assert composer.value == pasted[:-2] + "9"
    rows = composer.render()
    assert all(visible_width(row) <= 20 for row in rows)
    cursor = composer.cursor_position
    assert cursor is not None
    row, column = cursor
    assert 0 <= row < len(rows) and 0 <= column < 20
    assert "line-9" in rows[row]


def test_vertical_cursor_uses_display_cells_for_tabs_and_escaped_controls() -> None:
    theme = resolve_theme(color_system=None)
    for first_line in ("\tX", "\x1bX"):
        composer = Composer(
            theme=theme,
            width=40,
            max_height=3,
            style="borderless",
            gutter="none",
            padding="compact",
        )
        text = f"{first_line}\nabcd"
        composer.set_text(text)
        composer.handle_key(Key("up"))
        rows = composer.render()
        assert composer.value == text
        assert composer.cursor_position == (0, 4)
        if first_line.startswith("\x1b"):
            assert "\\x1b" in rows[0]
        composer.handle_key(Key("down"))
        composer.render()
        assert composer.value == text
        assert composer.cursor_position == (1, 4)


def test_full_width_cursor_uses_empty_soft_wrap_row_and_stays_visible() -> None:
    theme = resolve_theme(color_system=None)
    for max_height, expected_position in ((2, (1, 0)), (1, (0, 0))):
        composer = Composer(
            theme=theme,
            width=6,
            max_height=max_height,
            style="borderless",
            gutter="none",
            padding="compact",
        )
        composer.set_text("abcdef")
        rows = list(composer.render())
        assert composer.value == "abcdef"
        cursor = composer.cursor_position
        assert cursor is not None
        assert cursor == expected_position
        cursor_row, cursor_column = cursor
        assert cursor_row < max_height
        assert not rows[cursor_row].strip()
        assert 0 <= cursor_column < 6
        assert all(visible_width(row) <= 6 for row in rows)

    boxed = Composer(
        theme=theme,
        width=8,
        max_height=2,
        style="box",
        gutter="none",
        padding="compact",
    )
    boxed.set_text("abcdef")
    rows = boxed.render()
    assert boxed.value == "abcdef"
    cursor = boxed.cursor_position
    assert cursor is not None
    assert not rows[cursor[0]].replace(theme.symbol("boxRound.vertical"), "").strip()
    assert 0 < cursor[1] < 7
    assert rows[cursor[0]][cursor[1]] == " "


def test_band_composer_preserves_transparent_statusline_background() -> None:
    theme = resolve_theme("voidcode-dark", "dark", color_system="truecolor")
    status = StatusLine(
        theme,
        40,
        RuntimeTuiStatuslineConfig(layout="minimal", transparent=True),
    ).render(StatusSegmentData(state="ready", model="model", mode="ask"))
    composer = Composer(
        theme=theme,
        width=40,
        max_height=1,
        style="band",
        gutter="none",
        padding="compact",
    )

    assert "\x1b[49m" in status
    assert "\x1b[48;" not in "\n".join(composer.render(status=status))
