"""Component-local style + plaintext boundary regressions (no theme system)."""

from __future__ import annotations

import re

import pytest
from rich.color import ColorSystem

from voidcode.sdk.commands import ConfirmationRequest
from voidcode.sdk.views import (
    ComponentStyle,
    DiffViewData,
    FieldRow,
    FieldsViewData,
    NoticeViewData,
    StatusViewData,
    validate_view_data,
)
from voidcode.tui.styles import Styles
from voidcode.tui.views import render_diff, render_fields, render_notice, render_status

PLAIN = re.compile(r"\x1b\[[0-9;]*m")


def _plain(rows: list[str]) -> str:
    return "\n".join(PLAIN.sub("", row) for row in rows)


def test_explicit_foreground_tints_owning_component_content() -> None:
    rows = render_status(
        StatusViewData(label="T", value="1", style=ComponentStyle(foreground="bright_magenta")),
        40,
        Styles(ColorSystem.STANDARD),
    )
    assert "95m" in "".join(rows)
    assert "* T: 1" in _plain(rows)


def test_tone_maps_to_fixed_slot_and_level_still_prefixes() -> None:
    rows = render_status(
        StatusViewData(label="T", value="1", style=ComponentStyle(tone="warning")),
        40,
        Styles(ColorSystem.STANDARD),
    )
    assert "33m" in "".join(rows)  # fixed yellow slot, not plugin bytes
    notice = render_notice(NoticeViewData(message="boom", level="error"), 40, Styles(ColorSystem.STANDARD))
    assert "91m" in "".join(notice)


def test_no_color_strips_all_sgr_but_keeps_text() -> None:
    rows = render_status(
        StatusViewData(label="T", value="1", style=ComponentStyle(tone="error", foreground="#ff0000")),
        40,
        Styles(None),
    )
    assert not any("\x1b" in row for row in rows)
    assert "* T: 1" in _plain(rows)


def test_rich_markup_and_ansi_and_osc_are_literal() -> None:
    styles = Styles(ColorSystem.STANDARD)
    rows = render_fields(
        FieldsViewData(rows=(FieldRow(label="[red]L", value="v\x1b[31mX"),)),
        40,
        styles,
    )
    plain = _plain(rows)
    assert "[red]L" in plain  # Table cells must not parse markup
    assert "\\x1b[31m" in plain  # raw SGR visible, never executed
    assert "\x1b[31mX" not in "".join(rows)
    notice = render_notice(NoticeViewData(message="a\x1b]0;evil\x07b"), 40, styles)
    joined = "".join(notice)
    assert "\x1b]0;" not in joined
    assert "]0;evil" in _plain(notice)


def test_diff_semantic_cues_survive_component_style() -> None:
    rows = render_diff(
        DiffViewData(before="a", after="b", path="p", style=ComponentStyle(foreground="bright_magenta")),
        40,
        Styles(ColorSystem.STANDARD),
    )
    joined = "".join(rows)
    assert "\x1b[32m+b\x1b[0m" in joined
    assert "\x1b[91m-a\x1b[0m" in joined
    assert "95m" in joined  # heading/context carry the component tint


def test_diff_view_numbers_both_sides() -> None:
    rows = render_diff(DiffViewData(before="a", after="b", path="p"), 40, Styles(None))
    text = _plain(rows)
    assert "1   -a" in text
    assert " 1 +b" in text


def test_style_ingress_accepts_bare_color_and_rejects_injection() -> None:
    data = StatusViewData.from_payload({"label": "t", "value": "v", "style": {"foreground": "#12ab34"}})
    assert data.style == ComponentStyle(tone="default", foreground="#12ab34", background=None)
    assert validate_view_data("status", {"label": "t", "value": "v"})["style"] is None
    for bad_style in (
        {"tone": "bogus"},
        {"foreground": "\x1b[31m"},
        {"foreground": "red", "blink": True},
        {"foreground": "#12345"},
        {"foreground": "#12ab34\n"},
        {"tone": None},
        "red",
    ):
        with pytest.raises(ValueError):
            validate_view_data("status", {"label": "t", "value": "v", "style": bad_style})


def test_confirmation_ui_data_shares_style_contract() -> None:
    req = ConfirmationRequest(
        request_id="r",
        owner="o",
        command_name="c",
        arguments={},
        ui_type="confirmation",
        ui_data={"request_id": "r", "message": "m", "style": {"tone": "warning"}},
    )
    assert req.ui_data["style"] == {"tone": "warning", "foreground": None, "background": None}
