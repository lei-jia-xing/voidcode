import pytest

from voidcode.tui.composer import Composer
from voidcode.tui.term import visible_width
from voidcode.tui.theme import resolve_theme


@pytest.mark.parametrize("style", ["box", "band", "claude", "pi", "borderless", "rule", "field", "rail"])
@pytest.mark.parametrize(("gutter", "padding"), [("prompt", "normal"), ("none", "compact")])
def test_composer_profile_styles_respect_cell_width(style: str, gutter: str, padding: str) -> None:
    theme = resolve_theme("voidcode-dark", mode="dark", color_system=None)
    composer = Composer(theme=theme, width=12, max_height=3, style=style, gutter=gutter, padding=padding)
    text = "  \t日本語e\u0301👩\u200d💻"
    composer.set_text(text)

    rows = composer.render()

    assert composer.value == text
    assert all("\t" not in row and visible_width(row) <= 12 for row in rows)
    assert composer.cursor_position is not None
    row, column = composer.cursor_position
    assert 0 <= row < len(rows)
    assert 0 <= column < 12
