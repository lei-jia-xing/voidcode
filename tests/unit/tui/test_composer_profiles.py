import pytest

from voidcode.tui.composer import Composer
from voidcode.tui.styles import Styles
from voidcode.tui.term import visible_width


@pytest.mark.parametrize("style", ["box", "band", "claude", "pi", "borderless", "rule", "field", "rail"])
@pytest.mark.parametrize(("gutter", "padding"), [("prompt", "normal"), ("none", "compact")])
def test_composer_profile_styles_respect_cell_width(style: str, gutter: str, padding: str) -> None:
    composer = Composer(styles=Styles(None), width=12, max_height=3, style=style, gutter=gutter, padding=padding)
    text = "  \t日本語e\u0301👩\u200d💻"
    composer.set_text(text)

    rows = composer.render()

    assert composer.value == text
    assert all("\t" not in row and visible_width(row) <= 12 for row in rows)
    assert composer.cursor_position is not None
    row, column = composer.cursor_position
    assert 0 <= row < len(rows)
    assert 0 <= column < 12


def test_completion_window_follows_selection() -> None:
    from voidcode.tui.keys import Key

    composer = Composer(styles=Styles(None), width=40)
    composer.set_completion(tuple(f"/command-{i}" for i in range(20)))
    for _ in range(12):
        composer.handle_key(Key("down"))
    rows = composer.completion_rows(max_rows=4)
    assert len(rows) == 4
    assert "/command-12" in rows[-1]
    composer.handle_key(Key("enter"))
    assert composer.value == "/command-12"
