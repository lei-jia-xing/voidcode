"""Composer frame and input-row rendering for every configured style profile."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from .composer_text import _display_width, _wrap_display_line
from .formatting import pad_visible_row
from .styles import symbol
from .term import clamp_row, escape_terminal_controls, visible_width

if TYPE_CHECKING:
    from .composer import Composer


@dataclass(frozen=True, slots=True)
class _ComposerFrame:
    top_rule: bool = False
    bottom_rule: bool = False
    sides: bool = False
    sidebar: bool = False
    rail: bool = False
    status_dock: Literal["top", "bottom", "band"] = "top"


_FRAMES: Final = {
    "box": _ComposerFrame(top_rule=True, bottom_rule=True, sides=True),
    "band": _ComposerFrame(status_dock="band"),
    "claude": _ComposerFrame(top_rule=True, bottom_rule=True),
    "pi": _ComposerFrame(bottom_rule=True, status_dock="bottom"),
    "borderless": _ComposerFrame(),
    "rule": _ComposerFrame(top_rule=True),
    "field": _ComposerFrame(sidebar=True),
    "rail": _ComposerFrame(rail=True, status_dock="bottom"),
}


class ComposerRendering:
    """Status dock, frame rules, wrapped input rows, and popup rows."""

    def _prompt_gutter(self: Composer) -> str:
        if self._gutter != "prompt":
            return ""
        if self.is_bash_mode:
            return f"{self._styles.fg('accent', '!')} "
        return f"{symbol('nav.cursor')} "

    def _empty_content(self: Composer, layout_width: int) -> str:
        return clamp_row(self._styles.fg("dim", self._placeholder), layout_width) if self._placeholder else ""

    def _input_row(self: Composer, gutter: str, content: str, layout_width: int, width: int) -> str:
        frame = _FRAMES[self._style]
        vertical = symbol("boxRound.vertical")
        content = pad_visible_row(content, layout_width)
        parts = []
        if frame.sides or frame.sidebar:
            parts.append(self._styles.fg("border", vertical))
        elif frame.rail:
            parts.append(self._styles.fg("accent", vertical))
        if gutter:
            parts.append(self._styles.fg("border", gutter))
        if frame.sidebar:
            parts.append(self._styles.fg("border", vertical))
        parts.append(content)
        if frame.sides:
            parts.append(self._styles.fg("border", vertical))
        return clamp_row("".join(parts), width)

    def _horizontal_rule(self: Composer, *, corners: bool = False, overlay: str = "") -> str:
        horizontal = symbol("boxRound.horizontal")
        top_left = symbol("boxRound.topLeft")
        top_right = symbol("boxRound.topRight")
        inner_width = max(0, self._width - 2 if corners else self._width)
        inner = horizontal * inner_width
        if overlay and visible_width(overlay) + 2 <= inner_width:
            inner = f" {overlay} " + inner[visible_width(overlay) + 2 :]
        line = f"{top_left}{inner}{top_right}" if corners and self._width >= 2 else inner
        return clamp_row(self._styles.fg("border", line), self._width)

    def _status_row(self: Composer, status: str) -> str | None:
        if not status:
            return None
        frame = _FRAMES[self._style]
        width = self.status_width()
        vertical = symbol("boxRound.vertical")
        row = clamp_row(status, width)
        if frame.status_dock == "band":
            row = pad_visible_row(row, width)
            row = vertical + row + vertical
            return clamp_row(row, self._width)
        if frame.sides:
            row = self._styles.fg("border", vertical) + row + self._styles.fg("border", vertical)
        elif frame.sidebar:
            row = self._styles.fg("border", vertical) + row
        return clamp_row(row, self._width)

    def _style_rows(self: Composer, rows: list[str], status: str, border_status: str = "") -> list[str]:
        frame = _FRAMES[self._style]
        status_row = self._status_row(status)
        padded = self._padding == "normal"
        vertical = symbol("boxRound.vertical")
        padding_row = self._styles.fg("border", vertical + " " * max(0, self._width - 2) + vertical) if frame.sides else ""
        output: list[str] = []
        if frame.top_rule:
            output.append(self._horizontal_rule(corners=frame.sides, overlay=border_status))
        if padded:
            output.append(padding_row)
        if status_row is not None and frame.status_dock != "bottom":
            output.append(status_row)
        input_start = len(output)
        output.extend(rows)
        if status_row is not None and frame.status_dock == "bottom":
            output.append(status_row)
        if padded:
            output.append(padding_row)
        if frame.bottom_rule:
            output.append(
                clamp_row(
                    self._styles.fg(
                        "border",
                        symbol("boxRound.bottomLeft") + symbol("boxRound.horizontal") * max(0, self._width - 2) + symbol("boxRound.bottomRight"),
                    ),
                    self._width,
                )
                if frame.sides
                else self._horizontal_rule()
            )
        if frame.status_dock == "band" and status_row is None:
            output.append(self._horizontal_rule())
        if self._cursor_position is not None:
            row, column = self._cursor_position
            inset = int(frame.sides or frame.sidebar or frame.rail) + int(frame.sidebar)
            self._cursor_position = row + input_start, min(self._width - 1, column + inset)
        return [clamp_row(row, self._width) for row in output]

    def _input_rows(self: Composer, status: str, border_status: str = "") -> list[str]:
        frame = _FRAMES[self._style]
        inset = int(frame.sides) * 2 + int(frame.sidebar) * 2 + int(frame.rail)
        width = max(1, self._width - inset)
        gutter = self._prompt_gutter()
        gutter_width = min(visible_width(gutter), max(0, width - 2))
        if gutter_width <= 0:
            first_gutter = continuation = ""
        else:
            first_gutter = clamp_row(gutter, gutter_width)
            continuation = " " * gutter_width
        layout_width = max(2, width - gutter_width)

        if self._text == "":
            self._cursor_position = (0, gutter_width)
            row = self._input_row(first_gutter, self._empty_content(layout_width), layout_width, self._width)
            return self._style_rows([row], status, border_status)

        display_text, display_cursor = self._display_text_and_cursor()
        lines = display_text.split("\n")
        offsets: list[int] = []
        position = 0
        for line in lines:
            offsets.append(position)
            position += len(line) + 1

        line_index = display_text.count("\n", 0, display_cursor)
        local = display_cursor - offsets[line_index]
        cell = _display_width(lines[line_index][:local])

        wrapped = [_wrap_display_line(line, layout_width) for line in lines]
        _current_rows, current_starts = wrapped[line_index]
        cursor_row = bisect_right(current_starts, cell) - 1
        cursor_cell = cell - current_starts[cursor_row]
        flat = [(line, row) for line, (segment, _starts) in enumerate(wrapped) for row in range(len(segment))]
        cursor_flat = sum(len(segment) for segment, _starts in wrapped[:line_index]) + cursor_row
        visible_height = max(1, self._max_height)
        total = len(flat)
        if total <= visible_height:
            self._scroll = 0
        else:
            if cursor_flat < self._scroll:
                self._scroll = cursor_flat
            elif cursor_flat >= self._scroll + visible_height:
                self._scroll = cursor_flat - visible_height + 1
            self._scroll = max(0, min(self._scroll, total - visible_height))

        rows: list[str] = []
        for visible_index, (li, ri) in enumerate(flat[self._scroll : self._scroll + visible_height]):
            text = wrapped[li][0][ri]
            gutter_text = first_gutter if visible_index == 0 else continuation
            rows.append(self._input_row(gutter_text, text, layout_width, self._width))
        visible_cursor = cursor_flat - self._scroll
        self._cursor_position = (visible_cursor, min(width - 1, gutter_width + cursor_cell)) if 0 <= visible_cursor < len(rows) else None
        return self._style_rows(rows, status, border_status)

    def render(self: Composer, status: str = "", *, border_status: str = "") -> Sequence[str]:
        """Compose one actual status dock, frame, visible input rows, and cursor."""
        return self._input_rows(status, border_status)

    def completion_rows(self: Composer, *, max_rows: int = 6) -> Sequence[str]:
        """Popup rows above the composer (chrome only, never committed)."""
        rows: list[str] = []
        start = max(0, self._completion_index - max(1, max_rows) + 1)
        for index in range(start, min(len(self._completion), start + max(1, max_rows))):
            candidate = self._completion[index]
            description = self.completion_descriptions.get(candidate, "")
            group = " ›" if candidate in self.completion_groups else ""
            label = escape_terminal_controls(candidate + group + (f" — {description}" if description else ""))
            if index == self._completion_index:
                rows.append(self._styles.render("accent", f"{symbol('nav.cursor')} {label}", bold=True, reverse=True))
            else:
                rows.append(self._styles.fg("text", f"  {label}"))
        return [clamp_row(row, self._width) for row in rows]
