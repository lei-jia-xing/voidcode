"""ANSI-aware text metrics, wrapping, and row layout for the inline TUI."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from functools import lru_cache
from io import StringIO

import regex
from rich.cells import cell_len
from rich.color import ColorSystem
from rich.console import Console, OverflowMethod, RenderableType
from rich.theme import Theme as RichTheme

from .styles import color_system_name
from .term_ansi import RESET_STYLE

__all__ = [
    "clamp_row",
    "crop_row_right",
    "escape_terminal_controls",
    "format_number",
    "grapheme_spans",
    "render_lines",
    "visible_width",
    "wrap_plain",
    "wrap_row",
]

#: Height handed to the off-screen render console: rich only honours an explicit
#: ``width`` when both dimensions are set. Rows are never cropped by it.
_RENDER_HEIGHT = 1000


# ---------------------------------------------------------------------------
# ANSI-aware width layer
# ---------------------------------------------------------------------------


def _escape_end(text: str, start: int) -> int:
    """Return the index just past the escape sequence starting at ``start``."""
    length = len(text)
    index = start + 1
    if index >= length:
        return length
    kind = text[index]
    if kind == "[":
        index += 1
        while index < length and not ("@" <= text[index] <= "~"):
            index += 1
        return min(index + 1, length)
    if kind == "]":
        index += 1
        while index < length:
            if text[index] == "\x07":
                return index + 1
            if text[index] == "\x1b" and index + 1 < length and text[index + 1] == "\\":
                return index + 2
            index += 1
        return length
    return min(index + 1, length)


def _runs(text: str) -> Iterator[tuple[bool, str]]:
    """Split ``text`` into ``(is_escape, run)`` pairs; escapes are zero width."""
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "\x1b":
            end = text.find("\x1b", index)
            if end == -1:
                end = length
            yield False, text[index:end]
            index = end
            continue
        end = _escape_end(text, index)
        yield True, text[index:end]
        index = end


def grapheme_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Unicode extended-grapheme boundaries as Python string offsets."""
    return tuple((match.start(), match.end()) for match in regex.finditer(r"\X", text))


def _prefix_cells(text: str, width: int) -> str:
    """Longest whole-grapheme prefix whose display width fits."""
    if width <= 0:
        return ""
    taken: list[str] = []
    used = 0
    for start, end in grapheme_spans(text):
        grapheme = text[start:end]
        size = cell_len(grapheme)
        if used + size > width:
            break
        taken.append(grapheme)
        used += size
    return "".join(taken)


def visible_width(text: str) -> int:
    """Display width of ``text`` in terminal cells, ignoring ANSI sequences."""
    if "\x1b" not in text:
        return cell_len(text)
    total = 0
    for is_escape, run in _runs(text):
        if not is_escape:
            total += cell_len(run)
    return total


def escape_terminal_controls(
    text: str,
    *,
    preserve_newlines: bool = False,
    preserve_tabs: bool = False,
) -> str:
    """Make untrusted C0/C1 bytes visible before styling or markup parsing."""
    return "".join(
        char
        if (preserve_newlines and char == "\n") or (preserve_tabs and char == "\t")
        else f"\\x{ord(char):02x}"
        if ord(char) < 0x20 or ord(char) == 0x7F or 0x80 <= ord(char) <= 0x9F
        else char
        for char in text
    )


def clamp_row(text: str, width: int) -> str:
    """Truncate ``text`` to at most ``width`` cells, preserving ANSI styling.

    Empty when ``width <= 0``. The fast path (no escape sequences, already
    narrow enough) returns the input unchanged; a truncated styled row is
    closed with an SGR reset so it cannot bleed into the next row.
    """
    if width <= 0:
        return ""
    if "\x1b" not in text:
        return text if cell_len(text) <= width else _prefix_cells(text, width)
    taken: list[str] = []
    used = 0
    truncated = False
    for is_escape, run in _runs(text):
        if is_escape:
            taken.append(run)
            continue
        remaining = width - used
        if remaining <= 0:
            truncated = True
            break
        if cell_len(run) <= remaining:
            taken.append(run)
            used += cell_len(run)
            continue
        taken.append(_prefix_cells(run, remaining))
        truncated = True
        break
    if truncated:
        taken.append(RESET_STYLE)
    return "".join(taken)


def crop_row_right(text: str, width: int) -> str:
    """Keep the rightmost whole Unicode graphemes within ``width`` cells."""
    if width <= 0:
        return ""
    tokens: list[tuple[bool, str, int]] = []
    total = 0
    for is_escape, run in _runs(text):
        if is_escape:
            tokens.append((True, run, 0))
            continue
        for start, end in grapheme_spans(run):
            grapheme = run[start:end]
            size = cell_len(grapheme)
            tokens.append((False, grapheme, size))
            total += size
    if total <= width:
        return text
    remaining = width
    first = len(tokens)
    for index in range(len(tokens) - 1, -1, -1):
        is_escape, _token, size = tokens[index]
        if is_escape:
            continue
        if size > remaining:
            break
        remaining -= size
        first = index
        if remaining == 0:
            break
    prefix = "".join(token for index, (is_escape, token, _size) in enumerate(tokens) if is_escape and index < first)
    suffix = "".join(token for is_escape, token, _size in tokens[first:])
    if "\x1b" not in text:
        return suffix
    return RESET_STYLE + prefix + suffix + RESET_STYLE


_RENDER_LOCK = threading.Lock()


@lru_cache(maxsize=16)
def _render_console(width: int, theme: RichTheme | None, color_system: ColorSystem | None) -> Console:
    """One shared off-screen console per (width, theme, color) shape; callers swap in a fresh buffer per render."""
    console = Console(
        file=StringIO(),
        width=width,
        # rich's ``size`` ignores an explicit ``width`` when only one of
        # width/height is set and the console is forced to a terminal
        # (``rich/console.py`` ``size``); an explicit height keeps the width
        # authoritative. It never crops: rows are emitted one per logical line.
        height=_RENDER_HEIGHT,
        theme=theme,
        color_system=color_system_name(color_system),
        force_terminal=color_system is not None,
        highlight=False,
        emoji=False,
        soft_wrap=False,
        legacy_windows=False,
    )
    # The colour depth comes from the terminal's own detection (``Terminal``),
    # which already honours NO_COLOR / TERM=dumb; rich must not second-guess it
    # here (an off-screen console otherwise drops every style).
    console.no_color = False
    return console


def render_lines(
    renderable: RenderableType,
    width: int,
    *,
    theme: RichTheme | None = None,
    color_system: ColorSystem | None = ColorSystem.TRUECOLOR,
    overflow: OverflowMethod | None = None,
    no_wrap: bool = False,
) -> list[str]:
    """Render ``renderable`` at ``width`` and return it as ANSI row strings.

    Every row is guaranteed to be at most ``width`` cells wide: rich owns the
    wrapping (wide characters, emoji, grapheme clusters) and this module's
    :func:`clamp_row` is the final guard.
    """
    buffer = StringIO()
    console = _render_console(max(1, width), theme, color_system)
    with _RENDER_LOCK:
        console.file = buffer
        console.print(renderable, overflow=overflow, no_wrap=no_wrap)
        rendered = buffer.getvalue()
    rows = rendered.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    return rows


def wrap_row(
    text: str,
    width: int,
    *,
    theme: RichTheme | None = None,
    color_system: ColorSystem | None = ColorSystem.TRUECOLOR,
) -> list[str]:
    """Word-wrap a pre-styled row, preserving ANSI styling across the break.

    Rows that already carry styling (escaped terminal output, plugin-provided
    values) must not have an escape sequence sliced in half, so rich computes
    the break instead of a plain character split.
    """
    if width <= 0:
        return []
    from rich.text import Text

    return render_lines(Text.from_ansi(text), width, theme=theme, color_system=color_system)


def wrap_plain(text: str, width: int) -> list[str]:
    """Word-wrap unstyled text to terminal cells, hard-breaking long runs.

    Help/layout code wraps plain sanitized text before styling so continuation
    rows can carry an explicit hanging indent. Wide graphemes stay whole; the
    caller owns the indent (this function never adds one).
    """
    if width <= 0:
        return []
    remaining = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ").strip()
    if not remaining:
        return [""]
    rows: list[str] = []
    while visible_width(remaining) > width:
        prefix = _prefix_cells(remaining, width)
        if not prefix:
            spans = grapheme_spans(remaining)
            prefix = remaining[: spans[0][1]] if spans else remaining
        split = prefix.rfind(" ")
        if split > 0:
            row = remaining[:split].rstrip()
            remaining = remaining[split + 1 :].lstrip()
        else:
            row = prefix.rstrip()
            remaining = remaining[len(prefix) :].lstrip()
        if row:
            rows.append(row)
        if not remaining:
            break
    if remaining:
        rows.append(remaining.rstrip())
    return rows or [""]


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------


def format_number(value: int | float) -> str:
    """``999 / 1.5K / 25K / 1.5M`` (``packages/utils/src/format.ts:34-48``)."""
    number = float(value)
    if number < 1_000:
        return str(int(number))
    if number < 10_000:
        return f"{_trim1(number / 1_000)}K"
    if number < 1_000_000:
        return f"{round(number / 1_000)}K"
    if number < 10_000_000:
        return f"{_trim1(number / 1_000_000)}M"
    if number < 1_000_000_000:
        return f"{round(number / 1_000_000)}M"
    if number < 10_000_000_000:
        return f"{_trim1(number / 1_000_000_000)}B"
    return f"{round(number / 1_000_000_000)}B"


def _trim1(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text
