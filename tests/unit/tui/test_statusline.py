from __future__ import annotations

from dataclasses import replace

import pytest

from voidcode.tui.statusline import (
    StatusLine,
    StatusSegmentData,
    format_context_usage,
    format_number,
)
from voidcode.tui.term import visible_width

from .conftest import plain, theme

WIDTH = 100

FULL = StatusSegmentData(
    state="Running",
    model="voidcode-v1",
    thinking="medium",
    mode="suggest",
    path="/home/hunter/Workspace/voidcode",
    session_name="4f2a-voidcode",
    cost_usd=0.12,
    context_percent=42.5,
    context_window=200_000,
    context_tokens=85_000,
)


def render(data: StatusSegmentData, *, width: int = WIDTH, preset: str = "unicode") -> str:
    return StatusLine(theme(preset=preset), width).render(data)


def test_status_line_fills_the_width_and_wears_the_band_background() -> None:
    line = render(FULL)
    assert visible_width(line) == WIDTH
    assert line.startswith("\x1b[48;2;15;18;22m")
    assert plain(line).startswith(" Running")
    # Wide enough for both groups: the bar is padded to the full width.
    wide = render(FULL, width=200)
    assert visible_width(wide) == 200
    assert plain(wide).endswith(" ")


def test_segments_hide_when_they_have_no_data() -> None:
    only_state = render(StatusSegmentData(state="Idle"))
    assert "Idle" in plain(only_state)
    assert "$" not in plain(only_state)
    assert "◫" not in plain(only_state)
    assert render(StatusSegmentData()) == ""


def test_model_segment_carries_the_thinking_level() -> None:
    """Every canonical reasoning effort renders its own glyph."""
    assert "⬢ voidcode-v1 · ◑ med" in plain(render(FULL))
    for level, glyph in (
        ("off", "⦸ off"),
        ("minimal", "○ min"),
        ("low", "◔ low"),
        ("high", "◒ high"),
        ("xhigh", "◕ xhigh"),
        ("max", "◉ max"),
    ):
        assert f"⬢ voidcode-v1 · {glyph}" in plain(render(replace(FULL, thinking=level))), level


def test_path_segment_shortens_home_and_clamps_to_max_length() -> None:
    line = render(replace(FULL, path="/home/hunter/Workspace/voidcode"), width=200)
    assert "~/Workspace/voidcode" in plain(line)
    long_path = plain(render(replace(FULL, path="/home/hunter/" + "nested/" * 20), width=200))
    assert "…" in long_path


def test_context_segment_formats_and_colors_by_usage_band() -> None:
    normal = render(replace(FULL, context_percent=42.5), width=200)
    warning = render(replace(FULL, context_percent=55.0), width=200)
    error = render(replace(FULL, context_percent=95.0), width=200)
    assert "◫ 42.5%/200K" in plain(normal)
    assert "◫ 55.0%/200K" in plain(warning)
    assert "◫ 95.0%/200K" in plain(error)
    # The icon inherits the band's text colour; the value carries the band colour.
    assert "◫ \x1b[38;2;156;163;176m42.5%/200K" in normal  # statusLineContext
    assert "◫ \x1b[38;2;255;179;71m55.0%/200K" in warning  # warning
    assert "◫ \x1b[38;2;255;71;87m95.0%/200K" in error  # error


def test_unknown_context_window_renders_tokens_over_a_question_mark() -> None:
    assert format_context_usage(None, 0, 12_000) == "12K/?"
    assert "◫ 12K/?" in plain(render(replace(FULL, context_percent=None, context_window=0, context_tokens=12_000)))


def test_cost_segment_hides_at_zero_and_formats_two_decimals() -> None:
    assert "$0.12" in plain(render(FULL))
    assert "$" not in plain(render(replace(FULL, cost_usd=0.0)))


def test_lsp_segment_is_voidcodes_own_and_hides_when_absent() -> None:
    """The old sidebar's LSP panel survives as an optional right-group segment."""
    assert "lsp" not in plain(render(FULL, width=200))

    line = render(replace(FULL, lsp="lsp 2"), width=200)
    assert visible_width(line) == 200
    # Appended after the session name, so it is the first right segment popped.
    body = plain(line)
    assert body.index("4f2a-voidcode") < body.index("lsp 2")


def test_powerline_thin_separators_and_end_caps_come_from_the_glyphs() -> None:
    line = render(FULL, width=200)
    body = plain(line)
    # powerline-thin: ">" between left segments, end caps on both groups.
    assert " Running > ⬢ voidcode-v1 · ◑ med > suggest > " in body
    # End caps are drawn in the band's own background colour (useBgAsFg).
    assert "\x1b[38;2;15;18;22m▶\x1b[0m" in line
    assert "\x1b[38;2;15;18;22m◀\x1b[0m" in line


def test_ascii_glyph_preset_swaps_separators_and_icons() -> None:
    body = plain(render(FULL, preset="ascii", width=200))
    assert "ctx:" in body and "[M]" in body and "[D]" in body
    assert ">" in body and "◀" not in body


def test_right_group_is_dropped_before_left_segments_on_overflow() -> None:
    wide = render(FULL, width=200)
    narrow = render(FULL, width=110)
    assert "4f2a-voidcode" in plain(wide)
    assert "4f2a-voidcode" not in plain(narrow)
    assert "Running" in plain(narrow)
    assert "voidcode-v1" in plain(narrow)
    assert visible_width(narrow) == 110


def test_path_is_the_last_left_segment_dropped() -> None:
    body = plain(render(FULL, width=80))
    assert "Running" in body
    assert "voidcode" in body  # the workspace stays visible as long as possible
    assert "$0.12" not in body


@pytest.mark.parametrize("width", [40, 60, 80, 100, 140, 200])
def test_bar_never_exceeds_the_width(width: int) -> None:
    assert visible_width(render(FULL, width=width)) <= width


def test_gap_carries_the_embedded_context_gauge() -> None:
    body = plain(render(FULL, width=200))
    assert "42%" in body and "200K" in body
    assert "─" in body


def test_format_number_port() -> None:
    assert format_number(999) == "999"
    assert format_number(1_500) == "1.5K"
    assert format_number(25_000) == "25K"
    assert format_number(1_500_000) == "1.5M"
