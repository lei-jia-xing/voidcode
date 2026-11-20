"""Pure composer: an input editor state machine plus its row renderer.

Implements pure composer contracts:
editing, history, Unicode wrapping, cursor placement, and all eight named
frames. A closed frame table keeps layout geometry independent from editor
state, statusline profile, prompt gutter, and cursor. The app supplies
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

from .formatting import pad_visible_row
from .keys import Key
from .styles import Styles, symbol
from .term import cell_len, clamp_row, escape_terminal_controls, grapheme_spans, visible_width

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
#: Paste-block chords (reserved in terminal_app; pure view-state toggles, no I/O).
_EXPAND_KEYS = frozenset({"alt+e", "alt+shift+e"})
_COPY_KEYS = frozenset({"alt+c", "alt+shift+c"})
_DELETE_BLOCK_KEYS = frozenset({"alt+x", "alt+shift+x"})


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
    COPY = "copy"


@dataclass(frozen=True, slots=True)
class ComposerOutcome:
    """Result of :meth:`Composer.handle_key`."""

    action: ComposerAction = ComposerAction.NONE
    text: str = ""


@dataclass(slots=True)
class _PasteBlock:
    """One folded pasted span: exact true-text offsets plus its collapsed view flag."""

    start: int
    end: int
    collapsed: bool = True


def _badge_for_text(block_text: str) -> str:
    """Single-row display badge derived from a block's exact true text."""

    lines = block_text.count("\n") + 1
    nbytes = len(block_text.encode("utf-8"))
    return f"[pasted {lines} lines, {nbytes} bytes; Alt+E]"


def _bounds_in(text: str, cursor: int) -> tuple[int, int]:
    """Start and end offsets of the logical line holding ``cursor`` in ``text``."""

    start = text.rfind("\n", 0, cursor) + 1
    end = text.find("\n", cursor)
    return start, len(text) if end == -1 else end


def _cursor_up_in(text: str, cursor: int) -> int:
    """Cursor one logical line up in ``text``, preserving the display column."""

    start, _ = _bounds_in(text, cursor)
    if start == 0:
        return cursor
    column = _display_width(text[start:cursor])
    previous_end = start - 1
    previous_start = text.rfind("\n", 0, previous_end) + 1
    return _offset_at_cell(text, previous_start, previous_end, column)


def _cursor_down_in(text: str, cursor: int) -> int:
    """Cursor one logical line down in ``text``, preserving the display column."""

    start, end = _bounds_in(text, cursor)
    if end == len(text):
        return cursor
    column = _display_width(text[start:cursor])
    next_start = end + 1
    next_end = text.find("\n", next_start)
    if next_end == -1:
        next_end = len(text)
    return _offset_at_cell(text, next_start, next_end, column)


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
        # ``display`` is ANSI-free (tabs became spaces, controls became literal
        # text), so this is exactly ``visible_width``'s fast path.
        width = cell_len(display)
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
            [(display[part_start:part_end], cell_len(display[part_start:part_end])) for part_start, part_end in grapheme_spans(display)]
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
        styles: Styles | None = None,
        width: int,
        placeholder: str = "",
        max_height: int = 10,
        style: str = "box",
        gutter: str = "prompt",
        padding: str = "normal",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._styles = styles if styles is not None else Styles()
        self._width = max(1, width)
        self._placeholder = placeholder
        self._max_height = max(1, max_height)
        self._style = style
        self._gutter = gutter
        self._padding = padding
        self._clock = time.monotonic if clock is None else clock
        self._text = ""
        self._cursor = 0
        self._history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""
        self._draft_blocks: list[_PasteBlock] = []
        self._paste_blocks: list[_PasteBlock] = []
        self._last_interrupt = float("-inf")
        self._scroll = 0
        self._cursor_position: tuple[int, int] | None = None
        self._completion: tuple[str, ...] = ()
        self._completion_index = 0
        self._completion_range: tuple[int, int] | None = None
        self.completion_descriptions: dict[str, str] = {}
        self.completion_groups: set[str] = set()

    @property
    def is_bash_mode(self) -> bool:
        """Whether the draft is a native `!` shell escape (upstream bash mode)."""
        return self._text.startswith("!")

    # -- accessors ---------------------------------------------------------

    @property
    def value(self) -> str:
        """Current draft text."""
        return self._text

    @property
    def cursor(self) -> int:
        """Caret offset in the draft (grapheme-snapped by editing keys)."""
        return self._cursor

    @property
    def cursor_position(self) -> tuple[int, int] | None:
        """Native cursor location in the most recently rendered composer frame."""
        return self._cursor_position

    @property
    def history(self) -> tuple[str, ...]:
        """The bounded submitted-prompt history, oldest first."""
        return tuple(self._history)

    @property
    def paste_blocks(self) -> tuple[tuple[int, int, bool], ...]:
        """Folded paste spans as ``(start, end, collapsed)`` in true-text offsets."""
        return tuple((block.start, block.end, block.collapsed) for block in self._paste_blocks)

    @property
    def display_text(self) -> str:
        """Draft with collapsed blocks replaced by their single-row badges."""
        text, _ = self._display_text_and_cursor()
        return text

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
        self._draft_blocks = []
        self._paste_blocks = []
        self._scroll = 0
        self.set_completion(())

    def set_text(self, text: str) -> None:
        """Replace the draft with ``text`` (history insertion); the caret lands at the end."""
        self._text = text
        self._cursor = len(text)
        self._history_index = None
        self._paste_blocks = []
        self._scroll = 0
        self.set_completion(())

    @staticmethod
    def owns_key(name: str) -> bool:
        """Whether the composer itself consumes this key name (never a catalog chord)."""
        return name in _KEYMAP

    @property
    def completion(self) -> tuple[str, ...]:
        """Open completion candidates (empty when no popup is showing)."""
        return self._completion

    @property
    def completion_index(self) -> int:
        """Highlighted candidate while the popup is open."""
        return self._completion_index

    @property
    def completion_range(self) -> tuple[int, int] | None:
        """Active token [start, end) range being completed."""
        return self._completion_range

    def set_completion(
        self,
        candidates: Sequence[str],
        index: int = 0,
        target_range: tuple[int, int] | None = None,
    ) -> None:
        """Open the popup over ``candidates`` (empty closes it)."""
        self._completion = tuple(candidates)
        self._completion_index = min(max(0, index), max(0, len(self._completion) - 1))
        self._completion_range = target_range if self._completion else None

    # -- editing primitives ------------------------------------------------

    # -- paste blocks --------------------------------------------------------

    def _layout_width(self) -> int:
        """Usable input cells after frame inset and prompt gutter."""
        frame = _FRAMES[self._style]
        inset = int(frame.sides) * 2 + int(frame.sidebar) * 2 + int(frame.rail)
        width = max(1, self._width - inset)
        gutter = self._prompt_gutter()
        gutter_width = min(visible_width(gutter), max(0, width - 2))
        return max(2, width - gutter_width)

    def _count_display_rows(self, text: str, layout_width: int) -> int:
        """Wrapped row count for ``text`` at ``layout_width`` (pure viewport math)."""
        total = 0
        for line in text.split("\n"):
            rows, _ = _wrap_display_line(line, layout_width)
            total += len(rows)
        return total

    def _should_fold_paste(self, paste_text: str) -> bool:
        """Fold only a multi-line paste taller than the visible input height."""
        if "\n" not in paste_text:
            return False
        visible = max(1, self._max_height)
        if paste_text.count("\n") + 1 > visible:
            return True
        return self._count_display_rows(paste_text, self._layout_width()) > visible

    def _collapsed_blocks(self) -> list[_PasteBlock]:
        """Collapsed blocks in true-offset order (the list is always sorted)."""
        return [block for block in self._paste_blocks if block.collapsed]

    def _display_text_and_cursor(self) -> tuple[str, int]:
        """Display text with badges plus the display offset for the true cursor."""
        collapsed = self._collapsed_blocks()
        if not collapsed:
            return self._text, self._cursor
        parts: list[str] = []
        display_cursor: int | None = None
        display_pos = 0
        prev_true = 0
        for block in collapsed:
            run = self._text[prev_true : block.start]
            if display_cursor is None and prev_true <= self._cursor <= block.start:
                display_cursor = display_pos + (self._cursor - prev_true)
            parts.append(run)
            display_pos += len(run)
            badge = _badge_for_text(self._text[block.start : block.end])
            if display_cursor is None and block.start < self._cursor < block.end:
                display_cursor = display_pos + len(badge)
            parts.append(badge)
            display_pos += len(badge)
            prev_true = block.end
        tail = self._text[prev_true:]
        if display_cursor is None and prev_true <= self._cursor <= len(self._text):
            display_cursor = display_pos + (self._cursor - prev_true)
        parts.append(tail)
        if display_cursor is None:
            display_cursor = display_pos + len(tail)
        return "".join(parts), display_cursor

    def _display_to_true(self, display_offset: int, old_true: int) -> int:
        """Map a display offset back to true text; inside a badge snap to an edge."""
        collapsed = self._collapsed_blocks()
        if not collapsed:
            return max(0, min(display_offset, len(self._text)))
        display_pos = 0
        prev_true = 0
        for block in collapsed:
            run_len = block.start - prev_true
            if display_offset <= display_pos + run_len:
                return prev_true + (display_offset - display_pos)
            display_pos += run_len
            badge_len = len(_badge_for_text(self._text[block.start : block.end]))
            if display_offset <= display_pos + badge_len:
                if display_offset == display_pos:
                    return block.start
                if display_offset == display_pos + badge_len:
                    return block.end
                return block.start if old_true <= block.start else block.end
            display_pos += badge_len
            prev_true = block.end
        tail_len = len(self._text) - prev_true
        if display_offset <= display_pos + tail_len:
            return prev_true + (display_offset - display_pos)
        return len(self._text)

    def _block_at_cursor(self) -> _PasteBlock | None:
        """Block under the caret; adjacent collapsed wins over expanded."""
        matching = [block for block in self._paste_blocks if block.start <= self._cursor <= block.end]
        if not matching:
            return None
        if len(matching) == 1:
            return matching[0]
        for block in matching:
            if block.collapsed:
                return block
        return matching[0]

    def _adjust_blocks_for_insert(self, pos: int, length: int) -> None:
        """Shift or grow spans for an insertion of ``length`` at ``pos``."""
        if length <= 0:
            return
        for block in self._paste_blocks:
            if pos <= block.start:
                block.start += length
                block.end += length
            elif pos < block.end:
                block.end += length

    def _adjust_blocks_for_delete(self, del_start: int, del_end: int) -> None:
        """Drop, truncate, or shift spans for the removed true range."""
        deleted = del_end - del_start
        if deleted <= 0:
            return
        kept: list[_PasteBlock] = []
        for block in self._paste_blocks:
            if block.end <= del_start:
                kept.append(block)
            elif block.start >= del_end:
                block.start -= deleted
                block.end -= deleted
                kept.append(block)
            elif del_start <= block.start and block.end <= del_end:
                continue
            elif block.start < del_start and del_end < block.end:
                block.end -= deleted
                kept.append(block)
            elif del_start <= block.start < del_end < block.end:
                remaining = block.end - del_end
                block.start = del_start
                block.end = del_start + remaining
                kept.append(block)
            elif block.start < del_start < block.end <= del_end:
                block.end = del_start
                kept.append(block)
            else:
                kept.append(block)
        self._paste_blocks = [block for block in kept if block.start < block.end]

    def _expand_delete_for_collapsed(self, del_start: int, del_end: int) -> tuple[int, int]:
        """Grow a true delete range to include every touched collapsed block."""
        if del_start >= del_end:
            return del_start, del_end
        new_start, new_end = del_start, del_end
        for block in self._paste_blocks:
            if not block.collapsed:
                continue
            if new_start < block.end and new_end > block.start:
                new_start = min(new_start, block.start)
                new_end = max(new_end, block.end)
        return new_start, new_end

    def _delete_true_range(self, del_start: int, del_end: int) -> None:
        """Delete one true range, collapsing atomic badges it touches."""
        if del_start >= del_end:
            return
        del_start, del_end = self._expand_delete_for_collapsed(del_start, del_end)
        self._text = self._text[:del_start] + self._text[del_end:]
        self._cursor = del_start
        self._adjust_blocks_for_delete(del_start, del_end)
        self._edited()

    def _insert_paste(self, raw_text: str) -> None:
        """Insert pasted text, folding only a viewport-tall multi-line fragment."""
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        pos = self._cursor
        for block in self._paste_blocks:
            if block.start < pos < block.end:
                self._insert(text)
                return
        should_fold = self._should_fold_paste(text)
        self._adjust_blocks_for_insert(pos, len(text))
        self._text = self._text[:pos] + text + self._text[pos:]
        self._cursor = pos + len(text)
        self._edited()
        if should_fold:
            self._paste_blocks.append(_PasteBlock(pos, pos + len(text), True))
            self._paste_blocks.sort(key=lambda item: item.start)

    def _toggle_block_at_cursor(self) -> None:
        """Expand a collapsed badge or collapse an expanded block under the caret."""
        block = self._block_at_cursor()
        if block is None:
            return
        if block.collapsed:
            block.collapsed = False
            return
        if block.start < self._cursor < block.end:
            if self._cursor - block.start < block.end - self._cursor:
                self._cursor = block.start
            else:
                self._cursor = block.end
        block.collapsed = True

    def _copy_block_at_cursor(self) -> str | None:
        """Exact true text of the block under the caret, if any."""
        block = self._block_at_cursor()
        if block is None:
            return None
        return self._text[block.start : block.end]

    def _delete_block_at_cursor(self) -> None:
        """Delete the whole true span of the block under the caret, if any."""
        block = self._block_at_cursor()
        if block is None:
            return
        self._delete_true_range(block.start, block.end)

    def _insert(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        pos = self._cursor
        self._adjust_blocks_for_insert(pos, len(text))
        self._text = self._text[:pos] + text + self._text[pos:]
        self._cursor = pos + len(text)
        self._edited()

    def _edited(self) -> None:
        self._history_index = None
        self.set_completion(())

    def _bounds(self) -> tuple[int, int]:
        """Start and end offsets of the logical line holding the cursor."""
        return _bounds_in(self._text, self._cursor)

    def _cursor_up(self) -> None:
        self._cursor = _cursor_up_in(self._text, self._cursor)

    def _cursor_down(self) -> None:
        self._cursor = _cursor_down_in(self._text, self._cursor)

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
            self._draft_blocks = [_PasteBlock(block.start, block.end, block.collapsed) for block in self._paste_blocks]
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self._text = self._history[self._history_index]
        self._cursor = 0
        self._paste_blocks = []

    def _history_down(self) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._text = self._history[self._history_index]
            self._paste_blocks = []
        else:
            self._history_index = None
            self._text = self._draft
            self._paste_blocks = [_PasteBlock(block.start, block.end, block.collapsed) for block in self._draft_blocks]
        self._cursor = len(self._text)

    # -- completion ----------------------------------------------------------

    def accept_completion_with(self, replacement: str) -> None:
        """Replace the target token (or draft) with ``replacement``."""
        if self._completion_range is not None:
            start, end = self._completion_range
            del_start, del_end = self._expand_delete_for_collapsed(start, end) if start < end else (start, end)
            if del_start < del_end:
                self._text = self._text[:del_start] + self._text[del_end:]
                self._adjust_blocks_for_delete(del_start, del_end)
            if replacement:
                self._adjust_blocks_for_insert(del_start, len(replacement))
                self._text = self._text[:del_start] + replacement + self._text[del_start:]
            self._cursor = del_start + len(replacement)
        else:
            self._text = replacement
            self._cursor = len(self._text)
            self._paste_blocks = []
        self.set_completion(())
        self._edited()

    def _accept_completion(self) -> None:
        """Replace the active token (or draft) with the highlighted candidate."""
        if not self._completion:
            return
        self.accept_completion_with(self._completion[self._completion_index])

    # -- key handling ------------------------------------------------------

    def handle_key(self, key: Key) -> ComposerOutcome:
        """Fold one key into the composer; report what the app should do."""
        name = key.name
        if name == "escape" and self._text == "!":
            # Upstream bash mode: Esc exits the empty `!` mode instead of
            # cancelling a turn.
            self.clear()
            return ComposerOutcome()
        handler = _KEYMAP.get(name)
        if handler is not None:
            return handler(self, key)
        if key.text and name not in _NON_TEXT_KEYS:
            self._insert(key.text)
        return ComposerOutcome()

    def _key_escape(self, _key: Key) -> ComposerOutcome:
        if self._completion:
            # The 50 ms lone-Esc disambiguation already ran in the app loop:
            # by the time this key arrives it is a real Esc, so cancel the
            # popup without cancelling the turn.
            self.set_completion(())
            return ComposerOutcome()
        return ComposerOutcome(ComposerAction.CANCEL_TURN)

    def _key_ctrl_c(self, _key: Key) -> ComposerOutcome:
        now = self._clock()
        if now - self._last_interrupt < _DOUBLE_INTERRUPT_SECONDS:
            self._last_interrupt = now
            return ComposerOutcome(ComposerAction.INTERRUPT)
        self.clear()
        self._last_interrupt = now
        return ComposerOutcome()

    def _key_tab(self, _key: Key) -> ComposerOutcome:
        """Tab: accept the highlighted candidate (arrows move first)."""
        if not self._completion:
            return ComposerOutcome()
        self._accept_completion()
        return ComposerOutcome()

    def _key_newline(self, _key: Key) -> ComposerOutcome:
        self._insert("\n")
        return ComposerOutcome()

    def _key_enter(self, _key: Key) -> ComposerOutcome:
        if self._completion:
            self._accept_completion()
            return ComposerOutcome()
        return self._submit()

    def _key_paste(self, key: Key) -> ComposerOutcome:
        self._insert_paste(key.text)
        return ComposerOutcome()

    def _key_expand(self, _key: Key) -> ComposerOutcome:
        self._toggle_block_at_cursor()
        return ComposerOutcome()

    def _key_copy(self, _key: Key) -> ComposerOutcome:
        copied = self._copy_block_at_cursor()
        if copied is None:
            return ComposerOutcome()
        return ComposerOutcome(ComposerAction.COPY, copied)

    def _key_delete_block(self, _key: Key) -> ComposerOutcome:
        self._delete_block_at_cursor()
        return ComposerOutcome()

    def _key_left(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        for block in collapsed:
            if self._cursor == block.end:
                self._cursor = block.start
                return ComposerOutcome()
        target = _previous_boundary(self._text, self._cursor)
        for block in collapsed:
            if block.start < target < block.end:
                target = block.start
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_right(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        for block in collapsed:
            if self._cursor == block.start:
                self._cursor = block.end
                return ComposerOutcome()
        target = _next_boundary(self._text, self._cursor)
        for block in collapsed:
            if block.start < target < block.end:
                target = block.end
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_up(self, _key: Key) -> ComposerOutcome:
        if self._completion:
            self._completion_index = (self._completion_index - 1) % len(self._completion)
            return ComposerOutcome()
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            display_start, _ = _bounds_in(display_text, display_cursor)
            if self._history_index is not None or display_start == 0:
                self._history_up()
            else:
                old_true = self._cursor
                new_display = _cursor_up_in(display_text, display_cursor)
                self._cursor = self._display_to_true(new_display, old_true)
            return ComposerOutcome()
        if self._history_index is not None or self._bounds()[0] == 0:
            self._history_up()
        else:
            self._cursor_up()
        return ComposerOutcome()

    def _key_down(self, _key: Key) -> ComposerOutcome:
        if self._completion:
            self._completion_index = (self._completion_index + 1) % len(self._completion)
            return ComposerOutcome()
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            _, display_end = _bounds_in(display_text, display_cursor)
            if self._history_index is not None or display_end == len(display_text):
                self._history_down()
            else:
                old_true = self._cursor
                new_display = _cursor_down_in(display_text, display_cursor)
                self._cursor = self._display_to_true(new_display, old_true)
            return ComposerOutcome()
        if self._history_index is not None or self._bounds()[1] == len(self._text):
            self._history_down()
        else:
            self._cursor_down()
        return ComposerOutcome()

    def _key_home(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            display_start, _ = _bounds_in(display_text, display_cursor)
            self._cursor = self._display_to_true(display_start, self._cursor)
        else:
            start, _ = self._bounds()
            self._cursor = start
        return ComposerOutcome()

    def _key_end(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            _, display_end = _bounds_in(display_text, display_cursor)
            self._cursor = self._display_to_true(display_end, self._cursor)
        else:
            _, end = self._bounds()
            self._cursor = end
        return ComposerOutcome()

    def _key_word_left(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        target = _snap_left(self._text, _word_left(self._text, self._cursor))
        for block in collapsed:
            if block.start < target < block.end:
                target = block.start
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_word_right(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        target = _snap_right(self._text, _word_right(self._text, self._cursor))
        for block in collapsed:
            if block.start < target < block.end:
                target = block.end
                break
        self._cursor = target
        return ComposerOutcome()

    def _key_backspace(self, _key: Key) -> ComposerOutcome:
        if self._cursor > 0:
            target = _previous_boundary(self._text, self._cursor)
            self._delete_true_range(target, self._cursor)
        return ComposerOutcome()

    def _key_delete(self, _key: Key) -> ComposerOutcome:
        if self._cursor < len(self._text):
            target = _next_boundary(self._text, self._cursor)
            self._delete_true_range(self._cursor, target)
        return ComposerOutcome()

    def _key_delete_word_back(self, _key: Key) -> ComposerOutcome:
        target = _snap_left(self._text, _word_left(self._text, self._cursor))
        if target != self._cursor:
            self._delete_true_range(target, self._cursor)
        return ComposerOutcome()

    def _key_delete_word_forward(self, _key: Key) -> ComposerOutcome:
        target = _snap_right(self._text, _word_right(self._text, self._cursor))
        if target != self._cursor:
            self._delete_true_range(self._cursor, target)
        return ComposerOutcome()

    def _key_ctrl_u(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            display_start, _ = _bounds_in(display_text, display_cursor)
            if display_cursor > display_start:
                old_true = self._cursor
                true_start = self._display_to_true(display_start, old_true)
                true_end = self._display_to_true(display_cursor, old_true)
                if true_end > true_start:
                    self._delete_true_range(true_start, true_end)
                else:
                    self._cursor = true_start
            return ComposerOutcome()
        start, _ = self._bounds()
        if self._cursor > start:
            self._text = self._text[:start] + self._text[self._cursor :]
            self._cursor = start
            self._edited()
        return ComposerOutcome()

    def _key_ctrl_k(self, _key: Key) -> ComposerOutcome:
        collapsed = self._collapsed_blocks()
        if collapsed:
            display_text, display_cursor = self._display_text_and_cursor()
            _, display_end = _bounds_in(display_text, display_cursor)
            if display_cursor < display_end:
                old_true = self._cursor
                true_start = self._display_to_true(display_cursor, old_true)
                true_end = self._display_to_true(display_end, old_true)
                if true_end > true_start:
                    self._delete_true_range(true_start, true_end)
            return ComposerOutcome()
        _, end = self._bounds()
        if self._cursor < end:
            self._text = self._text[: self._cursor] + self._text[end:]
            self._edited()
        return ComposerOutcome()

    def _submit(self) -> ComposerOutcome:
        text = self._text
        if not text.strip():
            return ComposerOutcome()
        self._push_history(text)
        self.clear()
        return ComposerOutcome(ComposerAction.SUBMIT, text)

    # -- rendering ---------------------------------------------------------

    def _prompt_gutter(self) -> str:
        if self._gutter != "prompt":
            return ""
        if self.is_bash_mode:
            return f"{self._styles.fg('accent', '!')} "
        return f"{symbol('nav.cursor')} "

    def _empty_content(self, layout_width: int) -> str:
        return clamp_row(self._styles.fg("dim", self._placeholder), layout_width) if self._placeholder else ""

    def _input_row(self, gutter: str, content: str, layout_width: int, width: int) -> str:
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

    def _horizontal_rule(self, *, corners: bool = False, overlay: str = "") -> str:
        horizontal = symbol("boxRound.horizontal")
        top_left = symbol("boxRound.topLeft")
        top_right = symbol("boxRound.topRight")
        inner_width = max(0, self._width - 2 if corners else self._width)
        inner = horizontal * inner_width
        if overlay and visible_width(overlay) + 2 <= inner_width:
            inner = f" {overlay} " + inner[visible_width(overlay) + 2 :]
        line = f"{top_left}{inner}{top_right}" if corners and self._width >= 2 else inner
        return clamp_row(self._styles.fg("border", line), self._width)

    def _status_row(self, status: str) -> str | None:
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

    def _style_rows(self, rows: list[str], status: str, border_status: str = "") -> list[str]:
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

    def _input_rows(self, status: str, border_status: str = "") -> list[str]:
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

    def render(self, status: str = "", *, border_status: str = "") -> Sequence[str]:
        """Compose one actual status dock, frame, visible input rows, and cursor."""
        return self._input_rows(status, border_status)

    def completion_rows(self, *, max_rows: int = 6) -> Sequence[str]:
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


#: Key name → handler: the whole composer dispatch as data. Names are disjoint
#: across singles and key sets, so every key resolves to exactly one handler;
#: the escape-with-"!" pre-check and the text fallback stay in handle_key.
_KEYMAP: Final[dict[str, Callable[[Composer, Key], ComposerOutcome]]] = {
    "escape": Composer._key_escape,
    "ctrl+c": Composer._key_ctrl_c,
    "tab": Composer._key_tab,
    "enter": Composer._key_enter,
    "paste": Composer._key_paste,
    "left": Composer._key_left,
    "right": Composer._key_right,
    "up": Composer._key_up,
    "down": Composer._key_down,
    "home": Composer._key_home,
    "ctrl+a": Composer._key_home,
    "end": Composer._key_end,
    "ctrl+e": Composer._key_end,
    "backspace": Composer._key_backspace,
    "delete": Composer._key_delete,
    "ctrl+d": Composer._key_delete,
    "ctrl+u": Composer._key_ctrl_u,
    "ctrl+k": Composer._key_ctrl_k,
    **{name: Composer._key_newline for name in _NEWLINE_KEYS},
    **{name: Composer._key_expand for name in _EXPAND_KEYS},
    **{name: Composer._key_copy for name in _COPY_KEYS},
    **{name: Composer._key_delete_block for name in _DELETE_BLOCK_KEYS},
    **{name: Composer._key_word_left for name in _WORD_LEFT_KEYS},
    **{name: Composer._key_word_right for name in _WORD_RIGHT_KEYS},
    **{name: Composer._key_delete_word_back for name in _DELETE_WORD_BACK_KEYS},
    **{name: Composer._key_delete_word_forward for name in _DELETE_WORD_FORWARD_KEYS},
}
