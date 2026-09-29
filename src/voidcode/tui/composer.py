"""Pure composer: an input editor state machine plus its row renderer.

Ports the subset of omp's editor the VoidCode TUI needs
(``.omo/plans/tui-input-spec.md`` §3-§9): editing bindings, multi-line growth
and word wrapping, the ``band`` composer shape, session history, and the
Esc / Ctrl+C interrupt semantics. Tab is deliberately unbound: the editor has
no completion source, so it inserts nothing; slash input is submitted verbatim
and resolved by the runtime, not by this module.

The class is deliberately I/O free: it never touches a terminal, ``rich``
renderables, or the runtime. ``handle_key`` consumes a decoded :class:`Key`
and returns a :class:`ComposerOutcome`; ``render`` returns the composer's own
rows (the visible input rows), each already wrapped to at most ``width`` cells.
Status-row placement and the runtime cancel/exit paths belong to the app.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .keys import Key
from .term import clamp_row, visible_width, wrap_row
from .theme import Theme

__all__ = ["Composer", "ComposerAction", "ComposerOutcome"]

#: omp ``DOUBLE_INTERRUPT_MS`` (``prompt/composer.ts:22``).
_DOUBLE_INTERRUPT_SECONDS = 0.5
#: omp session history depth (``editor.ts:915-919``).
_HISTORY_DEPTH = 100
#: Paste tab expansion -- omp expands to three spaces (``editor.ts:2930``).
_PASTE_TAB = "   "
#: The ``band`` shape's prompt gutter. omp hard-codes this literal
#: (``composer/band.ts:17`` ``defaultPromptGutter: "╰─ "``) -- it is NOT read
#: from the symbol preset, so it stays ``╰─ `` under ``glyph_preset="ascii"``.
_PROMPT_GUTTER = "╰─ "
#: Cells the empty-draft placeholder must keep clear of the caret
#: (omp ``PLACEHOLDER_MIN_GAP``, ``editor.ts:412``); below it the placeholder hides.
_PLACEHOLDER_MIN_GAP = 2

_NEWLINE_KEYS = frozenset({"shift+enter", "ctrl+j", "ctrl+enter", "alt+enter"})
_WORD_LEFT_KEYS = frozenset({"alt+left", "alt+b", "ctrl+left"})
_WORD_RIGHT_KEYS = frozenset({"alt+right", "alt+f", "ctrl+right"})
_DELETE_WORD_BACK_KEYS = frozenset({"ctrl+w", "alt+backspace", "ctrl+backspace"})
_DELETE_WORD_FORWARD_KEYS = frozenset({"alt+delete", "alt+d"})
#: Keys whose ``text`` is not plain inserted text.
_NON_TEXT_KEYS = frozenset({"unknown", "paste"})

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


def _index_at_cell(row: str, cell: int) -> int:
    """Index of the code point occupying column ``cell`` in a plain row."""
    used = 0
    for index, ch in enumerate(row):
        width = visible_width(ch)
        if used + width > cell:
            return index
        used += width
    return len(row)


def _sanitize_paste(text: str) -> str:
    """Normalise pasted text (omp ``#sanitizePastedText``, ``editor.ts:2910``)."""
    clean = text.replace("\r\n", "\n").replace("\r", "\n")
    clean = unicodedata.normalize("NFC", clean).replace("\t", _PASTE_TAB)
    return "".join(ch for ch in clean if ch == "\n" or 0x20 <= ord(ch) != 0x7F)


class Composer:
    """Pure editor state machine and row renderer."""

    def __init__(
        self,
        *,
        theme: Theme,
        width: int,
        placeholder: str = "",
        max_height: int = 10,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._theme = theme
        self._width = max(1, width)
        self._placeholder = placeholder
        self._max_height = max(1, max_height)
        self._clock = time.monotonic if clock is None else clock
        self._text = ""
        self._cursor = 0
        self._enabled = True
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self._last_interrupt = float("-inf")
        # ``_cursor_glyph`` reads this for the disabled composer's liveness
        # frame. Nothing advances it: the spinner is the app's, not the editor's.
        self._spinner_frame = 0
        self._scroll = 0

    # -- accessors ---------------------------------------------------------

    @property
    def value(self) -> str:
        """Current draft text."""
        return self._text

    @property
    def history(self) -> tuple[str, ...]:
        """The bounded submitted-prompt history, oldest first."""
        return tuple(self._history)

    def set_width(self, width: int) -> None:
        self._width = max(1, width)

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
        column = self._cursor - start
        previous_end = start - 1
        previous_start = self._text.rfind("\n", 0, previous_end) + 1
        self._cursor = min(previous_start + column, previous_end)

    def _cursor_down(self) -> None:
        start, end = self._bounds()
        if end == len(self._text):
            return
        column = self._cursor - start
        next_start = end + 1
        next_end = self._text.find("\n", next_start)
        if next_end == -1:
            next_end = len(self._text)
        self._cursor = min(next_start + column, next_end)

    # -- history -----------------------------------------------------------

    def _push_history(self, text: str) -> None:
        entry = text.strip()
        if not entry:
            return
        if self._history and self._history[-1] == entry:
            return
        self._history.append(entry)
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
            self._insert(_sanitize_paste(key.text))
            return ComposerOutcome()

        if name == "left":
            if self._cursor > 0:
                self._cursor -= 1
            return ComposerOutcome()
        if name == "right":
            if self._cursor < len(self._text):
                self._cursor += 1
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
            self._cursor = _word_left(self._text, self._cursor)
            return ComposerOutcome()
        if name in _WORD_RIGHT_KEYS:
            self._cursor = _word_right(self._text, self._cursor)
            return ComposerOutcome()

        if name == "backspace":
            if self._cursor > 0:
                self._text = self._text[: self._cursor - 1] + self._text[self._cursor :]
                self._cursor -= 1
                self._edited()
            return ComposerOutcome()
        if name in ("delete", "ctrl+d"):
            if self._cursor < len(self._text):
                self._text = self._text[: self._cursor] + self._text[self._cursor + 1 :]
                self._edited()
            return ComposerOutcome()
        if name in _DELETE_WORD_BACK_KEYS:
            target = _word_left(self._text, self._cursor)
            if target != self._cursor:
                self._text = self._text[:target] + self._text[self._cursor :]
                self._cursor = target
                self._edited()
            return ComposerOutcome()
        if name in _DELETE_WORD_FORWARD_KEYS:
            target = _word_right(self._text, self._cursor)
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
        """The ``band`` shape's ``╰─ `` cue (a literal, not a preset glyph)."""
        return _PROMPT_GUTTER

    def _cursor_glyph(self) -> str:
        """The end-of-line caret: omp ``symbols.inputCursor``, never ``nav.cursor``.

        A disabled composer shows the liveness spinner instead (the turn owns the
        keyboard, and the caret means nothing while it does).
        """
        if not self._enabled:
            frames = self._theme.spinner_frames("status")
            if frames:
                return self._theme.fg("accent", frames[self._spinner_frame % len(frames)])
        symbol = self._theme.input_cursor()
        return self._theme.fg("accent", symbol) if symbol else ""

    def _decorate_cursor(self, row: str, cell: int, row_width: int, layout_width: int) -> str:
        """Place the caret inside one content row (omp ``editor.ts:1434-1483``).

        Mid-row the caret is the grapheme under it in reverse video; at
        end-of-line it is the thin ``inputCursor`` glyph. A row with no spare
        cell borrows its last grapheme -- underlined, so insertion after the last
        character stays distinct from insertion before it (``editor.ts:1167-1212``).
        """
        if cell < row_width:
            index = _index_at_cell(row, cell)
            return row[:index] + "\x1b[7m" + row[index] + "\x1b[0m" + row[index + 1 :]
        glyph = self._cursor_glyph()
        if row_width + visible_width(glyph) <= layout_width:
            return row + glyph
        if not row:
            return clamp_row(glyph, layout_width)
        # Row is full: borrow the cells of its last grapheme, underlined (not
        # reverse video) so insertion after the last character stays visually
        # distinct from insertion before it -- omp's
        # ``#renderEndOfLineCursorAtWidthLimit`` (``editor.ts:1167-1212``).
        index = _index_at_cell(row, row_width - 1)
        return row[:index] + "\x1b[4m" + row[index:] + "\x1b[0m"

    def _empty_content(self, layout_width: int) -> str:
        """The empty-draft row: caret at the insertion point, placeholder beside it.

        omp keeps the caret first and paints the placeholder flush right,
        separated by at least ``PLACEHOLDER_MIN_GAP`` cells, and drops the
        placeholder when the gap cannot be honoured (``editor.ts:1312-1319``).
        """
        glyph = self._cursor_glyph()
        if not self._placeholder:
            return clamp_row(glyph, layout_width)
        caret_width = visible_width(glyph)
        gap = layout_width - caret_width - visible_width(self._placeholder)
        if gap < _PLACEHOLDER_MIN_GAP:
            return clamp_row(glyph, layout_width)
        return glyph + " " * gap + self._theme.fg("dim", self._placeholder)

    def _input_row(self, gutter: str, content: str, layout_width: int, width: int) -> str:
        pad = " " * max(0, layout_width - visible_width(content))
        gutter_cell = self._theme.fg("border", gutter) if gutter else ""
        return clamp_row(gutter_cell + content + pad, width)

    def _input_rows(self) -> list[str]:
        width = self._width
        gutter = self._prompt_gutter()
        gutter_width = min(visible_width(gutter), width)
        if gutter_width <= 0:
            first_gutter = continuation = ""
        else:
            first_gutter = clamp_row(gutter, gutter_width)
            continuation = " " * gutter_width
        layout_width = max(1, width - gutter_width)

        if self._text == "":
            return [self._input_row(first_gutter, self._empty_content(layout_width), layout_width, width)]

        lines = self._text.split("\n")
        offsets: list[int] = []
        position = 0
        for line in lines:
            offsets.append(position)
            position += len(line) + 1

        line_index = self._text.count("\n", 0, self._cursor)
        local = self._cursor - offsets[line_index]
        cell = visible_width(lines[line_index][:local])

        wrapped = [wrap_row(line, layout_width) or [""] for line in lines]
        widths = [[visible_width(row) for row in segment] for segment in wrapped]

        cursor_row = 0
        cursor_cell = 0
        used = 0
        for index, row_width in enumerate(widths[line_index]):
            if cell <= used + row_width:
                cursor_row = index
                cursor_cell = cell - used
                break
            used += row_width
        else:
            cursor_row = len(widths[line_index]) - 1
            cursor_cell = widths[line_index][-1]

        flat = [(line, row) for line, segment in enumerate(wrapped) for row in range(len(segment))]
        cursor_flat = sum(len(segment) for segment in widths[:line_index]) + cursor_row

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
            text = wrapped[li][ri]
            if (li, ri) == (line_index, cursor_row):
                text = self._decorate_cursor(text, cursor_cell, widths[li][ri], layout_width)
            gutter_text = first_gutter if visible_index == 0 else continuation
            rows.append(self._input_row(gutter_text, text, layout_width, width))
        return rows

    def render(self) -> Sequence[str]:
        """Composer rows: the visible input rows, wrapped to <= width."""
        return self._input_rows()
