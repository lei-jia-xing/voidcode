"""Pure composer: an input editor state machine plus its row renderer.

Implements pure composer contracts:
editing, history, Unicode wrapping, cursor placement, and all eight named
frames. A closed frame table keeps layout geometry independent from editor
state, statusline profile, theme, prompt gutter, and cursor. The app supplies
the one real statusline row; this module renders it in the selected dock.

The class is deliberately I/O free: it never touches a terminal, ``rich``
renderables, or the runtime. ``handle_key`` consumes a decoded :class:`Key`
and returns a :class:`ComposerOutcome`; ``render`` returns the composed
status/frame/input rows and the native insertion cursor position.
"""

from __future__ import annotations

import time
import unicodedata
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from .keys import Key
from .term import clamp_row, escape_terminal_controls, grapheme_spans, visible_width
from .theme import Theme

__all__ = ["Composer", "ComposerAction", "ComposerOutcome"]

#: omp ``DOUBLE_INTERRUPT_MS`` (``prompt/composer.ts:22``).
_DOUBLE_INTERRUPT_SECONDS = 0.5
#: omp session history depth (``editor.ts:915-919``).
_HISTORY_DEPTH = 100
#: The native terminal cursor is the sole insertion-point indicator.

_NEWLINE_KEYS = frozenset({"shift+enter", "ctrl+j", "ctrl+enter", "alt+enter"})
_WORD_LEFT_KEYS = frozenset({"alt+left", "alt+b", "ctrl+left"})
_WORD_RIGHT_KEYS = frozenset({"alt+right", "alt+f", "ctrl+right"})
_DELETE_WORD_BACK_KEYS = frozenset({"ctrl+w", "alt+backspace", "ctrl+backspace"})
_DELETE_WORD_FORWARD_KEYS = frozenset({"alt+delete", "alt+d"})
#: Keys whose ``text`` is not plain inserted text.
_NON_TEXT_KEYS = frozenset({"unknown", "paste"})


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

# Word-navigation kinds (omp ``getWordNavKind``, ``utils.ts:543``).
_WS = "whitespace"
_WORD = "word"
_DELIM = "delimiter"
_CJK = "cjk"
_OTHER = "other"


class ComposerAction(StrEnum):
    """What the app should do after a key press."""

    NONE = "none"
    SUBMIT = "submit"
    CANCEL_TURN = "cancel_turn"
    INTERRUPT = "interrupt"


@dataclass(frozen=True, slots=True)
class ComposerOutcome:
    """Result of :meth:`Composer.handle_key`."""

    action: ComposerAction = ComposerAction.NONE
    text: str = ""


def _is_cjk(cp: int) -> bool:
    return (
        0x3040 <= cp <= 0x30FF  # kana
        or 0x3400 <= cp <= 0x4DBF  # CJK ext A
        or 0x4E00 <= cp <= 0x9FFF  # CJK unified
        or 0xF900 <= cp <= 0xFAFF  # CJK compatibility
        or 0xAC00 <= cp <= 0xD7AF  # hangul
    )


def _classify(ch: str) -> str:
    if ch.isspace():
        return _WS
    if ch == "_":
        return _WORD
    if _is_cjk(ord(ch)):
        return _CJK
    if ch.isalnum():
        return _WORD
    if unicodedata.category(ch)[0] in ("P", "S"):
        return _DELIM
    return _OTHER


def _word_left(text: str, cursor: int) -> int:
    """One coarse word to the left (omp ``moveWordLeft``)."""
    i = cursor
    while i > 0 and _classify(text[i - 1]) == _WS:
        i -= 1
    if i == 0:
        return 0
    kind = _classify(text[i - 1])
    if kind in (_DELIM, _CJK):
        while i > 0 and _classify(text[i - 1]) == kind:
            i -= 1
        return i
    if kind == _WORD:
        while i > 0 and _classify(text[i - 1]) == _WORD:
            i -= 1
        return i
    return i - 1


def _word_right(text: str, cursor: int) -> int:
    """One coarse word to the right (omp ``moveWordRight``)."""
    n = len(text)
    i = cursor
    while i < n and _classify(text[i]) == _WS:
        i += 1
    if i >= n:
        return i
    kind = _classify(text[i])
    if kind in (_DELIM, _CJK):
        while i < n and _classify(text[i]) == kind:
            i += 1
        return i
    if kind == _WORD:
        while i < n and _classify(text[i]) == _WORD:
            i += 1
        return i
    return i + 1


def _previous_boundary(text: str, index: int) -> int:
    return max((start for start, end in grapheme_spans(text) if end <= index), default=0)


def _next_boundary(text: str, index: int) -> int:
    return next((end for start, end in grapheme_spans(text) if start >= index or start < index < end), len(text))


def _snap_left(text: str, index: int) -> int:
    return max((start for start, end in grapheme_spans(text) if start <= index), default=0)


def _snap_right(text: str, index: int) -> int:
    return min((end for start, end in grapheme_spans(text) if end >= index), default=len(text))


def _display_tokens(text: str) -> list[tuple[int, int, str, int]]:
    """Map raw graphemes to the same safe cell widths used by the editor."""
    tokens: list[tuple[int, int, str, int]] = []
    column = 0
    for start, end in grapheme_spans(text):
        grapheme = text[start:end]
        if grapheme == "\t":
            display = " " * (4 - column % 4)
        else:
            display = escape_terminal_controls(grapheme)
        width = visible_width(display)
        tokens.append((start, end, display, width))
        column += width
    return tokens


def _display_width(text: str) -> int:
    return sum(width for _start, _end, _display, width in _display_tokens(text))


def _offset_at_cell(text: str, start: int, end: int, cell: int) -> int:
    used = 0
    for local_start, _local_end, _display, width in _display_tokens(text[start:end]):
        if used + width > cell:
            return start + local_start
        used += width
    return end


def _wrap_display_line(text: str, width: int) -> tuple[list[str], list[int]]:
    """Hard-wrap editor cells and retain each row's logical display-column origin."""
    rows: list[str] = []
    starts: list[int] = []
    parts: list[str] = []
    row_width = 0
    row_start = 0
    column = 0
    for _start, _end, display, token_width in _display_tokens(text):
        pieces = (
            [(display[part_start:part_end], visible_width(display[part_start:part_end])) for part_start, part_end in grapheme_spans(display)]
            if token_width > width
            else [(display, token_width)]
        )
        for piece, piece_width in pieces:
            if row_width and row_width + piece_width > width:
                rows.append("".join(parts))
                starts.append(row_start)
                parts.clear()
                row_width = 0
                row_start = column
            parts.append(piece)
            row_width += piece_width
            column += piece_width
            if row_width >= width:
                rows.append("".join(parts))
                starts.append(row_start)
                parts.clear()
                row_width = 0
                row_start = column
    if parts:
        rows.append("".join(parts))
        starts.append(row_start)
    elif not rows:
        rows.append("")
        starts.append(0)
    elif row_start == column:
        rows.append("")
        starts.append(column)
    return rows, starts


class Composer:
    """Pure editor state machine and row renderer."""

    def __init__(
        self,
        *,
        theme: Theme,
        width: int,
        placeholder: str = "",
        max_height: int = 10,
        style: str = "band",
        gutter: str = "prompt",
        padding: str = "normal",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._theme = theme
        self._width = max(1, width)
        self._placeholder = placeholder
        self._max_height = max(1, max_height)
        self._style = style
        self._gutter = gutter
        self._padding = padding
        self._clock = time.monotonic if clock is None else clock
        self._text = ""
        self._cursor = 0
        self._enabled = True
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self._last_interrupt = float("-inf")
        self._scroll = 0
        self._cursor_position: tuple[int, int] | None = None

    # -- accessors ---------------------------------------------------------

    @property
    def value(self) -> str:
        """Current draft text."""
        return self._text

    @property
    def cursor_position(self) -> tuple[int, int] | None:
        """Native cursor location in the most recently rendered composer frame."""
        return self._cursor_position

    def set_appearance(self, *, theme: Theme, style: str, gutter: str, padding: str) -> None:
        self._theme = theme
        self._style = style
        self._gutter = gutter
        self._padding = padding
        self._cursor_position = None

    @property
    def history(self) -> tuple[str, ...]:
        """The bounded submitted-prompt history, oldest first."""
        return tuple(self._history)

    def set_max_height(self, height: int) -> None:
        self._max_height = max(1, height)

    def set_width(self, width: int) -> None:
        self._width = max(1, width)

    def status_width(self) -> int:
        """Width reserved for the single app-owned statusline row."""
        frame = _FRAMES[self._style]
        inset = 2 if frame.sides or frame.status_dock == "band" else 1 if frame.sidebar else 0
        return max(1, self._width - inset)

    def input_height(self, terminal_height: int, *, status_visible: bool = True) -> int:
        """Input capacity after fixed frame, padding, and status-dock rows."""
        frame = _FRAMES[self._style]
        fixed = int(frame.top_rule) + int(frame.bottom_rule)
        if self._padding == "normal":
            fixed += 2
        if status_visible:
            fixed += 1
        return max(1, min(10, terminal_height - fixed))

    def clear(self) -> None:
        """Drop the draft and any history browsing state."""
        self._text = ""
        self._cursor = 0
        self._history_index = None
        self._draft = ""
        self._scroll = 0

    def set_text(self, text: str) -> None:
        """Replace the draft with ``text`` (history insertion); the caret lands at the end."""
        self._text = text
        self._cursor = len(text)
        self._history_index = None
        self._scroll = 0

    def set_enabled(self, enabled: bool) -> None:
        """While disabled the composer ignores every key (streaming/overlay)."""
        self._enabled = enabled

    # -- editing primitives ------------------------------------------------

    def _insert(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        self._text = self._text[: self._cursor] + text + self._text[self._cursor :]
        self._cursor += len(text)
        self._edited()

    def _edited(self) -> None:
        self._history_index = None

    def _bounds(self) -> tuple[int, int]:
        """Start and end offsets of the logical line holding the cursor."""
        start = self._text.rfind("\n", 0, self._cursor) + 1
        end = self._text.find("\n", self._cursor)
        return start, len(self._text) if end == -1 else end

    def _cursor_up(self) -> None:
        start, _ = self._bounds()
        if start == 0:
            return
        column = _display_width(self._text[start : self._cursor])
        previous_end = start - 1
        previous_start = self._text.rfind("\n", 0, previous_end) + 1
        self._cursor = _offset_at_cell(self._text, previous_start, previous_end, column)

    def _cursor_down(self) -> None:
        start, end = self._bounds()
        if end == len(self._text):
            return
        column = _display_width(self._text[start : self._cursor])
        next_start = end + 1
        next_end = self._text.find("\n", next_start)
        if next_end == -1:
            next_end = len(self._text)
        self._cursor = _offset_at_cell(self._text, next_start, next_end, column)

    # -- history -----------------------------------------------------------

    def _push_history(self, text: str) -> None:
        if not text.strip():
            return
        if self._history and self._history[-1] == text:
            return
        self._history.append(text)
        if len(self._history) > _HISTORY_DEPTH:
            del self._history[0]

    def _history_up(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._draft = self._text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self._text = self._history[self._history_index]
        self._cursor = 0

    def _history_down(self) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._text = self._history[self._history_index]
        else:
            self._history_index = None
            self._text = self._draft
        self._cursor = len(self._text)

    # -- key handling ------------------------------------------------------

    def handle_key(self, key: Key) -> ComposerOutcome:
        """Fold one key into the composer; report what the app should do."""
        if not self._enabled:
            return ComposerOutcome()
        name = key.name

        if name == "escape":
            return ComposerOutcome(ComposerAction.CANCEL_TURN)
        if name == "ctrl+c":
            return self._handle_ctrl_c()
        if name in _NEWLINE_KEYS:
            self._insert("\n")
            return ComposerOutcome()
        if name == "enter":
            return self._submit()
        if name == "paste":
            self._insert(key.text)
            return ComposerOutcome()

        if name == "left":
            self._cursor = _previous_boundary(self._text, self._cursor)
            return ComposerOutcome()
        if name == "right":
            self._cursor = _next_boundary(self._text, self._cursor)
            return ComposerOutcome()
        if name == "up":
            if self._history_index is not None or self._bounds()[0] == 0:
                self._history_up()
            else:
                self._cursor_up()
            return ComposerOutcome()
        if name == "down":
            if self._history_index is not None or self._bounds()[1] == len(self._text):
                self._history_down()
            else:
                self._cursor_down()
            return ComposerOutcome()

        start, end = self._bounds()
        if name in ("home", "ctrl+a"):
            self._cursor = start
            return ComposerOutcome()
        if name in ("end", "ctrl+e"):
            self._cursor = end
            return ComposerOutcome()
        if name in _WORD_LEFT_KEYS:
            self._cursor = _snap_left(self._text, _word_left(self._text, self._cursor))
            return ComposerOutcome()
        if name in _WORD_RIGHT_KEYS:
            self._cursor = _snap_right(self._text, _word_right(self._text, self._cursor))
            return ComposerOutcome()

        if name == "backspace":
            if self._cursor > 0:
                target = _previous_boundary(self._text, self._cursor)
                self._text = self._text[:target] + self._text[self._cursor :]
                self._cursor = target
                self._edited()
            return ComposerOutcome()
        if name in ("delete", "ctrl+d"):
            if self._cursor < len(self._text):
                target = _next_boundary(self._text, self._cursor)
                self._text = self._text[: self._cursor] + self._text[target:]
                self._edited()
            return ComposerOutcome()
        if name in _DELETE_WORD_BACK_KEYS:
            target = _snap_left(self._text, _word_left(self._text, self._cursor))
            if target != self._cursor:
                self._text = self._text[:target] + self._text[self._cursor :]
                self._cursor = target
                self._edited()
            return ComposerOutcome()
        if name in _DELETE_WORD_FORWARD_KEYS:
            target = _snap_right(self._text, _word_right(self._text, self._cursor))
            if target != self._cursor:
                self._text = self._text[: self._cursor] + self._text[target:]
                self._edited()
            return ComposerOutcome()
        if name == "ctrl+u":
            if self._cursor > start:
                self._text = self._text[:start] + self._text[self._cursor :]
                self._cursor = start
                self._edited()
            return ComposerOutcome()
        if name == "ctrl+k":
            if self._cursor < end:
                self._text = self._text[: self._cursor] + self._text[end:]
                self._edited()
            return ComposerOutcome()

        if key.text and name not in _NON_TEXT_KEYS:
            self._insert(key.text)
        return ComposerOutcome()

    def _submit(self) -> ComposerOutcome:
        text = self._text
        if not text.strip():
            return ComposerOutcome()
        self._push_history(text)
        self.clear()
        return ComposerOutcome(ComposerAction.SUBMIT, text)

    def _handle_ctrl_c(self) -> ComposerOutcome:
        now = self._clock()
        if now - self._last_interrupt < _DOUBLE_INTERRUPT_SECONDS:
            self._last_interrupt = now
            return ComposerOutcome(ComposerAction.INTERRUPT)
        self.clear()
        self._last_interrupt = now
        return ComposerOutcome()

    # -- rendering ---------------------------------------------------------

    def _prompt_gutter(self) -> str:
        return f"{self._theme.symbol('nav.cursor')} " if self._gutter == "prompt" else ""

    def _empty_content(self, layout_width: int) -> str:
        return clamp_row(self._theme.fg("dim", self._placeholder), layout_width) if self._placeholder else ""

    def _input_row(self, gutter: str, content: str, layout_width: int, width: int) -> str:
        frame = _FRAMES[self._style]
        vertical = self._theme.symbol("boxRound.vertical") or "|"
        pad = " " * max(0, layout_width - visible_width(content))
        parts = []
        if frame.sides or frame.sidebar:
            parts.append(self._theme.fg("border", vertical))
        elif frame.rail:
            parts.append(self._theme.fg("accent", vertical))
        if gutter:
            parts.append(self._theme.fg("border", gutter))
        if frame.sidebar:
            parts.append(self._theme.fg("border", vertical))
        parts.append(content + pad)
        if frame.sides:
            parts.append(self._theme.fg("border", vertical))
        return clamp_row("".join(parts), width)

    def _horizontal_rule(self, *, corners: bool = False) -> str:
        horizontal = self._theme.symbol("boxRound.horizontal") or "-"
        if corners and self._width >= 2:
            line = self._theme.symbol("boxRound.topLeft") + horizontal * (self._width - 2) + self._theme.symbol("boxRound.topRight")
        else:
            line = horizontal * self._width
        return clamp_row(self._theme.fg("border", line), self._width)

    def _status_row(self, status: str) -> str | None:
        if not status:
            return None
        frame = _FRAMES[self._style]
        width = self.status_width()
        vertical = self._theme.symbol("boxRound.vertical") or "|"
        row = clamp_row(status, width)
        if frame.status_dock == "band":
            row += " " * max(0, width - visible_width(row))
            row = vertical + row + vertical
            return clamp_row(row, self._width)
        if frame.sides:
            row = self._theme.fg("border", vertical) + row + self._theme.fg("border", vertical)
        elif frame.sidebar:
            row = self._theme.fg("border", vertical) + row
        return clamp_row(row, self._width)

    def _style_rows(self, rows: list[str], status: str) -> list[str]:
        frame = _FRAMES[self._style]
        status_row = self._status_row(status)
        padded = self._padding == "normal"
        vertical = self._theme.symbol("boxRound.vertical") or "|"
        padding_row = self._theme.fg("border", vertical + " " * max(0, self._width - 2) + vertical) if frame.sides else ""
        output: list[str] = []
        if frame.top_rule:
            output.append(self._horizontal_rule(corners=frame.sides))
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
                    self._theme.fg(
                        "border",
                        self._theme.symbol("boxRound.bottomLeft")
                        + (self._theme.symbol("boxRound.horizontal") or "-") * max(0, self._width - 2)
                        + self._theme.symbol("boxRound.bottomRight"),
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

    def _input_rows(self, status: str) -> list[str]:
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
            self._cursor_position = (0, gutter_width) if self._enabled else None
            row = self._input_row(first_gutter, self._empty_content(layout_width), layout_width, self._width)
            return self._style_rows([row], status)

        lines = self._text.split("\n")
        offsets: list[int] = []
        position = 0
        for line in lines:
            offsets.append(position)
            position += len(line) + 1

        line_index = self._text.count("\n", 0, self._cursor)
        local = self._cursor - offsets[line_index]
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
        self._cursor_position = (
            (visible_cursor, min(width - 1, gutter_width + cursor_cell)) if self._enabled and 0 <= visible_cursor < len(rows) else None
        )
        return self._style_rows(rows, status)

    def render(self, status: str = "") -> Sequence[str]:
        """Compose one actual status dock, frame, visible input rows, and cursor."""
        return self._input_rows(status)
