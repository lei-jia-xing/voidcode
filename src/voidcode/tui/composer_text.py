"""Cell-safe composer text layout: grapheme navigation, wrapping, and paste badges."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .term import cell_len, escape_terminal_controls, grapheme_spans

# Word-navigation kinds (omp ``getWordNavKind``, ``utils.ts:543``).
_WS = "whitespace"
_WORD = "word"
_DELIM = "delimiter"
_CJK = "cjk"
_OTHER = "other"


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
