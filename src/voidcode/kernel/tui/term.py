"""Terminal I/O for the inline renderer.

This module owns every byte the TUI puts on the terminal, plus the ANSI-aware
text layer that guarantees a row never exceeds the terminal width.

Rendering model (mirrors oh-my-pi's ``HistoryBatch``/viewport split):

* :meth:`Terminal.commit_rows` writes finalized rows **once** into the native
  scrollback. They are never rewritten, erased, or audited afterwards.
* :meth:`Terminal.paint_frame` repaints the bounded live region below them,
  per-row diffed against the previous frame, using relative cursor movement and
  erase-to-EOL. Identical frames emit **zero bytes**.
* Both writes are wrapped in synchronized output (``?2026h``/``?2026l``) with
  the cursor hidden and autowrap off, so a frame is atomic on screen.
* Full-screen overlays borrow the alternate screen only when its original state
  is known off; entry first reanchors the main-buffer caret.

The live region is anchored at an owned origin. Before frame edits, commits,
and resets, ``Terminal`` returns the cached native caret to that origin. After
``paint_frame`` the terminal places a supplied native caret at its frame cell;
without one, the cursor remains hidden at the region's first row. After
``commit_rows`` it sits at column 1 of the first row below committed rows,
where the live region starts. Committed rows are written top-down and the
terminal scrolls when they overflow, pushing only already committed rows into
scrollback (the live region is erased first, so an unfinished frame can never
be pushed).

When stdout is not a TTY or raw mode cannot be entered, ``paint_frame`` appends
changed rows instead of moving or hiding the cursor. Renderer styling may remain
when colour support was detected; a ``None`` colour system independently omits
styling while preserving terminal controls on an available raw TTY.
"""

from __future__ import annotations

import ctypes
import importlib
import os
import select
import signal
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from io import StringIO
from typing import Any, Literal

import regex
from rich.cells import cell_len
from rich.color import ColorSystem
from rich.console import Console, OverflowMethod, RenderableType
from rich.theme import Theme as RichTheme

from .region import TuiFrame, diff_rows
from .theme import color_system_name

termios: Any = importlib.import_module("termios") if os.name != "nt" else None
tty: Any = importlib.import_module("tty") if os.name != "nt" else None

__all__ = [
    "TerminalCapabilityError",
    "Terminal",
    "copy_to_clipboard",
    "crop_row_right",
    "escape_terminal_controls",
    "format_number",
    "render_lines",
    "visible_width",
    "wrap_row",
    "grapheme_spans",
]

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET_STYLE = "\x1b[0m"
SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
AUTOWRAP_OFF = "\x1b[?7l"
AUTOWRAP_ON = "\x1b[?7h"
ERASE_TO_END_OF_LINE = "\x1b[2K"
ERASE_BELOW = "\x1b[J"
CARRIAGE_RETURN = "\r"

#: Full-screen overlays may borrow it; transcript and inline live content stay
#: in the main buffer, preserving native scrollback.
ALTERNATE_SCREEN_ON = "\x1b[?1049h"
ALTERNATE_SCREEN_OFF = "\x1b[?1049l"

# Session-scoped terminal protocols. Terminal consumes private-mode query
# replies before user input; the keyboard enhancement probe is fire-and-forget.
# Its replies decode as unknown keys, which the composer does not insert.
BRACKETED_PASTE_ON = "\x1b[?2004h"
BRACKETED_PASTE_OFF = "\x1b[?2004l"
KEYBOARD_PROBE = "\x1b[?u\x1b[c"
KEYBOARD_ENABLE = "\x1b[>5u"
KEYBOARD_POP = "\x1b[<u"


_DEFAULT_SIZE = (80, 24)
_READ_CHUNK = 4096
#: Height handed to the off-screen render console: rich only honours an explicit
#: ``width`` when both dimensions are set. Rows are never cropped by it.
_RENDER_HEIGHT = 1000
_BACKGROUND_RESPONSE = regex.compile(rb"\x1b\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})(?:\x07|\x1b\\)")
_PRIVATE_MODE_RESPONSE = regex.compile(rb"\x1b\[\?(\d+);([0-4])\$y")
_CURSOR_STYLE_RESPONSE = regex.compile(rb"\x1bP1\$r(?:q)?([0-6]) q\x1b\\")


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
    console = Console(
        file=buffer,
        width=max(1, width),
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


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


def _fileno(stream: Any) -> int | None:
    try:
        return stream.fileno()
    except AttributeError, OSError, ValueError:
        return None


def _detect_size(fd: int | None) -> tuple[int, int]:
    if fd is not None:
        try:
            size = os.get_terminal_size(fd)
        except OSError, ValueError:
            size = None
        if size is not None:
            return max(1, size.columns), max(1, size.lines)
    try:
        return max(1, int(os.environ["COLUMNS"])), max(1, int(os.environ["LINES"]))
    except KeyError, ValueError:
        return _DEFAULT_SIZE


def _detect_color_system(is_tty: bool) -> ColorSystem | None:
    if os.environ.get("NO_COLOR") is not None:
        return None
    if not is_tty:
        return None
    term = os.environ.get("TERM", "")
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return ColorSystem.TRUECOLOR
    if os.name == "nt" and os.environ.get("WT_SESSION"):
        return ColorSystem.TRUECOLOR
    if term in ("", "dumb"):
        return ColorSystem.STANDARD if os.name == "nt" else None
    if "256" in term:
        return ColorSystem.EIGHT_BIT
    return ColorSystem.STANDARD


def _detect_unicode() -> bool:
    if os.name == "nt":
        return True
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


def _colorfgbg_rgb() -> tuple[int, int, int] | None:
    fields = os.environ.get("COLORFGBG", "").split(";")
    try:
        index = int(fields[-1])
    except ValueError, IndexError:
        return None
    if not 0 <= index <= 255:
        return None
    if index < 16:
        colors = (
            (0, 0, 0),
            (205, 0, 0),
            (0, 205, 0),
            (205, 205, 0),
            (0, 0, 238),
            (205, 0, 205),
            (0, 205, 205),
            (229, 229, 229),
            (127, 127, 127),
            (255, 0, 0),
            (0, 255, 0),
            (255, 255, 0),
            (92, 92, 255),
            (255, 0, 255),
            (0, 255, 255),
            (255, 255, 255),
        )
        return colors[index]
    if index < 232:
        levels = (0, 95, 135, 175, 215, 255)
        offset = index - 16
        return levels[offset // 36], levels[(offset // 6) % 6], levels[offset % 6]
    gray = 8 + (index - 232) * 10
    return gray, gray, gray


def _osc_rgb(match: Any) -> tuple[int, int, int]:
    channels = [round(int(group, 16) * 255 / (16 ** len(group) - 1)) for group in match.groups()]
    return channels[0], channels[1], channels[2]


class TerminalCapabilityError(RuntimeError):
    """The requested native terminal behavior cannot be queried safely."""


class Terminal:
    """Single-writer terminal handle for the inline renderer.

    Construct one with :meth:`Terminal.open`. Instances are not reusable after
    :meth:`close`.
    """

    def __init__(
        self,
        *,
        input_fd: int | None = None,
        output_fd: int = 1,
        is_tty: bool = False,
        size: tuple[int, int] = _DEFAULT_SIZE,
        color_system: ColorSystem | None = None,
        unicode_ok: bool = True,
    ) -> None:
        self._input_fd = input_fd
        self._output_fd = output_fd
        self._is_tty = is_tty
        self._size = size
        self._color_system = color_system
        self._unicode_ok = unicode_ok
        self._lock = threading.RLock()
        self._painted: list[str] = []
        self._painted_width = 0
        self._painted_cursor: tuple[int, int] | None = None
        self._resize = False
        self._alt = False
        self._raw = False
        self._closed = False
        self._eof = False
        self._saved_termios: list[Any] | None = None
        self._previous_winch: Any = None
        self._previous_termination: dict[int, Any] = {}
        self._previous_suspend: Any = None
        self._windows_saved_modes: dict[int, int] = {}
        self._windows_console_handles: dict[int, int] = {}
        self._windows_kernel: Any = None
        self._suspended_alt = False
        self._pending_input = bytearray()
        self._windows_input_record_type: Any = None
        self._windows_pending_high_surrogate: int | None = None
        self._background_source: Literal["osc11", "COLORFGBG", "unavailable", "not queried"] = "not queried"
        self._keyboard_active = False
        self._enabled_private_modes: set[int] = set()
        self._unsupported_private_modes: set[int] = set()
        self._original_cursor_style: int | None = None
        self._current_cursor_style: int | None = None
        self._cursor_style_changed = False

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls) -> Terminal:
        """Open the process terminal in raw mode without borrowing the alternate screen.

        Degrades to an append-only log when stdout is not a TTY or raw mode is
        unavailable. Colour depth is independent; ``None`` disables styling,
        not terminal input or cursor control.
        """
        input_fd = _fileno(sys.stdin)
        output_fd = _fileno(sys.stdout)
        output_is_tty = output_fd is not None and os.isatty(output_fd)
        terminal = cls(
            input_fd=input_fd,
            output_fd=output_fd if output_fd is not None else 1,
            is_tty=output_is_tty,
            size=_detect_size(output_fd if output_is_tty else None),
            color_system=_detect_color_system(output_is_tty),
            unicode_ok=_detect_unicode(),
        )
        try:
            if output_is_tty:
                terminal._enter_raw_mode()
                if terminal.is_tty:
                    terminal._capture_private_modes()
                    terminal._enter_application_mode()
                    terminal._install_winch()
                    terminal._install_termination_handlers()
        except BaseException:
            terminal.close()
            raise
        return terminal

    # -- state -------------------------------------------------------------

    @property
    def width(self) -> int:
        """Current terminal width in cells."""
        return self._size[0]

    @property
    def height(self) -> int:
        """Current terminal height in rows."""
        return self._size[1]

    @property
    def is_tty(self) -> bool:
        """Whether frames are written as escape sequences to a real terminal."""
        return self._is_tty

    @property
    def color_system(self) -> ColorSystem | None:
        """Detected colour depth; ``None`` means "no colour"."""
        return self._color_system

    @property
    def unicode_ok(self) -> bool:
        """Whether the output encoding can carry non-ASCII glyphs."""
        return self._unicode_ok

    @property
    def eof(self) -> bool:
        """Whether the input side reported end of file."""
        return self._eof

    @property
    def background_source(self) -> Literal["osc11", "COLORFGBG", "unavailable", "not queried"]:
        """Source actually used by the most recent background probe."""
        return self._background_source

    @property
    def has_input(self) -> bool:
        """Whether an input descriptor exists to read from at all."""
        return self._input_fd is not None and not self._eof

    def set_cursor_shape(self, shape: str, blink: bool | None = None) -> None:
        """Set an explicit native cursor shape while retaining the session state."""
        if shape == "terminal":
            if self._cursor_style_changed and self._original_cursor_style is not None:
                self._set_cursor_style(self._original_cursor_style)
                self._cursor_style_changed = False
            return
        if not self._is_tty:
            return
        if shape not in {"bar", "block", "underline"}:
            raise ValueError(f"unsupported cursor shape: {shape}")
        if self._original_cursor_style is None:
            original = self._query_cursor_style()
            if original is None:
                raise TerminalCapabilityError("terminal did not report its cursor style; explicit cursor shape is unavailable")
            self._original_cursor_style = self._current_cursor_style = original
        original = self._original_cursor_style
        assert original is not None
        blink_state = (original in (0, 1, 3, 5)) if blink is None else blink
        base = {"block": 1, "underline": 3, "bar": 5}[shape]
        desired = base if blink_state else base + 1
        if desired != self._current_cursor_style:
            self._set_cursor_style(desired)
        self._cursor_style_changed = desired != original

    def _set_cursor_style(self, style: int) -> None:
        self._emit(f"\x1b[{style} q")
        self._current_cursor_style = style

    def _reapply_cursor_style(self) -> None:
        style = self._current_cursor_style
        if style is not None:
            self._current_cursor_style = None
            self._set_cursor_style(style)

    def _read_input_chunk(self, timeout: float | None) -> bytes | None:
        if self._input_fd is None:
            return b""
        if os.name == "nt":
            return self._read_windows_console_chunk(timeout)
        try:
            ready, _, _ = select.select([self._input_fd], [], [], timeout)
        except OSError, TypeError, ValueError:
            return b""
        if not ready:
            return None
        try:
            return os.read(self._input_fd, _READ_CHUNK)
        except OSError:
            return b""

    def _read_windows_console_chunk(self, timeout: float | None) -> bytes | None:
        record_type = self._windows_input_record_type
        if record_type is None:
            return b""
        handle = self._windows_console_handles.get(self._input_fd)
        if handle is None:
            return b""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            milliseconds = 0xFFFFFFFF if remaining is None else min(0xFFFFFFFE, int(remaining * 1000 + 0.999))
            wait_result = self._windows_kernel.WaitForSingleObject(handle, milliseconds)
            if wait_result == 0x102:
                return None
            if wait_result != 0:
                return b""
            record = record_type()
            count = ctypes.c_uint32()
            if not self._windows_kernel.ReadConsoleInputW(handle, ctypes.byref(record), 1, ctypes.byref(count)):
                return b""
            if not count.value or record.EventType != 1:
                continue
            key_event = record.Event.KeyEvent
            if not key_event.bKeyDown:
                continue
            codepoint = key_event.uChar.UnicodeChar
            if 0xD800 <= codepoint <= 0xDBFF:
                if self._windows_pending_high_surrogate is None:
                    self._windows_pending_high_surrogate = codepoint
                    continue
                self._windows_pending_high_surrogate = codepoint
                return "\ufffd".encode("utf-8")
            if 0xDC00 <= codepoint <= 0xDFFF:
                high = self._windows_pending_high_surrogate
                self._windows_pending_high_surrogate = None
                text = chr(0x10000 + ((high - 0xD800) << 10) + codepoint - 0xDC00) if high is not None else "\ufffd"
                return (text * max(1, key_event.wRepeatCount)).encode("utf-8")
            replacement = "\ufffd" if self._windows_pending_high_surrogate is not None else ""
            self._windows_pending_high_surrogate = None
            text = self._windows_key_text(key_event, codepoint)
            if text or replacement:
                repeated = text * max(1, key_event.wRepeatCount)
                return (replacement + repeated).encode("utf-8")

    @staticmethod
    def _windows_key_text(key_event: Any, codepoint: int) -> str:
        control_state = key_event.dwControlKeyState
        modifiers = int(bool(control_state & 0x0010)) | (int(bool(control_state & 0x0003)) << 1) | (int(bool(control_state & 0x000C)) << 2)
        virtual_key = key_event.wVirtualKeyCode
        altgr = control_state & 0x0009 == 0x0009 and not control_state & 0x0006
        if codepoint:
            text = chr(codepoint)
            if altgr and text.isprintable():
                return text
            if not modifiers:
                return text
            if modifiers & 1 and 0x41 <= virtual_key <= 0x5A:
                base = chr(virtual_key + ord("a") - ord("A"))
                return f"\x1b[{ord(base)}:{codepoint};{modifiers + 1}u"
            if modifiers & 4 and 1 <= codepoint <= 26:
                codepoint += ord("a") - 1
            return f"\x1b[{codepoint};{modifiers + 1}u"
        navigation = {
            0x23: "F",
            0x24: "H",
            0x25: "D",
            0x26: "A",
            0x27: "C",
            0x28: "B",
        }
        final = navigation.get(virtual_key)
        if final is not None:
            return f"\x1b[1;{modifiers + 1}{final}" if modifiers else f"\x1b[{final}"
        tilde = {0x21: 5, 0x22: 6, 0x2D: 2, 0x2E: 3}.get(virtual_key)
        if tilde is not None:
            return f"\x1b[{tilde};{modifiers + 1}~" if modifiers else f"\x1b[{tilde}~"
        if 0x70 <= virtual_key <= 0x73:
            final = chr(ord("P") + virtual_key - 0x70)
            return f"\x1b[1;{modifiers + 1}{final}" if modifiers else f"\x1bO{final}"
        function_key = {
            0x74: 15,
            0x75: 17,
            0x76: 18,
            0x77: 19,
            0x78: 20,
            0x79: 21,
            0x7A: 23,
            0x7B: 24,
        }.get(virtual_key)
        if function_key is not None:
            return f"\x1b[{function_key};{modifiers + 1}~" if modifiers else f"\x1b[{function_key}~"
        if virtual_key in (0x09, 0x0D, 0x1B, 0x20, 0x08):
            codepoint = {0x09: 9, 0x0D: 13, 0x1B: 27, 0x20: 32, 0x08: 127}[virtual_key]
            if modifiers:
                return f"\x1b[{codepoint};{modifiers + 1}u"
            return chr(codepoint)
        if modifiers and 0x41 <= virtual_key <= 0x5A:
            return f"\x1b[{virtual_key + 32};{modifiers + 1}u"
        return ""

    def _query_cursor_style(self) -> int | None:
        if self._input_fd is None:
            return None
        self._emit("\x1bP$q q\x1b\\")
        received = bytearray()
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            chunk = self._read_input_chunk(max(0.0, deadline - time.monotonic()))
            if chunk is None:
                break
            if not chunk:
                self._eof = True
                break
            received.extend(chunk)
            match = _CURSOR_STYLE_RESPONSE.search(received)
            if match is not None:
                self._pending_input.extend(received[: match.start()])
                self._pending_input.extend(received[match.end() :])
                return int(match.group(1))
        self._pending_input.extend(received)
        return None

    @property
    def alt_screen(self) -> bool:
        """Whether the alternate screen buffer is currently borrowed."""
        return self._alt

    def resize_pending(self) -> bool:
        """Whether a native resize has arrived since the last frame."""
        if self._is_tty:
            self._refresh_size()
        return self._resize

    # -- writes ------------------------------------------------------------

    def write(self, text: str) -> None:
        """Write raw text with no frame brackets (single-writer, atomic call)."""
        if self._closed or not text:
            return
        self._emit(text)

    def commit_rows(self, rows: Sequence[str]) -> None:
        """Write finalized rows into the native scrollback, exactly once.

        Long rows are ANSI-aware wrapped to the terminal width before writing,
        so a settled command/model/error line can never lose its tail. Rows are
        written top-down and never rewritten. The live region is erased first so
        the scroll a full screen triggers can only push committed rows.
        """
        if self._closed:
            return
        rows = list(rows)
        if not rows:
            return
        if not self._is_tty:
            row_width = max(1, self.width)
            rows = [part for row in rows for part in wrap_row(row, row_width)]
            self._emit("".join(row + "\n" for row in (clamp_row(row, row_width) for row in rows)))
            return
        if self._geometry_stale():
            self._reset_live()
        row_width = self._frame_row_width()
        rows = [part for row in rows for part in wrap_row(row, row_width)]
        parts = [self._paint_begin(), self._cursor_to_live_origin()]
        if self._painted:
            parts.append(ERASE_BELOW)
        for row in rows:
            parts.append(clamp_row(row, row_width))
            parts.append(self._newline())
        parts.append(self._paint_end())
        self._emit("".join(parts))
        self._painted = []
        self._painted_cursor = None
        self._painted_width = self.width

    def paint_frame(self, frame: TuiFrame) -> None:
        """Paint one typed frame and place its optional native cursor."""
        if self._closed:
            return
        if self._geometry_stale():
            self._reset_live()
        rows = frame.rows
        if not self._is_tty:
            rendered = [clamp_row(row, self.width) for row in rows][-self.height :]
            if rendered == self._painted:
                return
            self._painted = rendered
            self._emit("".join(row + "\n" for row in rendered))
            return

        row_width = self._frame_row_width()
        rendered = [clamp_row(row, row_width) for row in rows]
        split = max(0, len(rendered) - self.height)
        rendered = rendered[split:]
        cursor = frame.cursor
        if cursor is not None:
            row, column = cursor
            cursor = (row - split, min(max(0, column), row_width - 1)) if split <= row < split + len(rendered) else None
        previous = self._painted
        changes = diff_rows(previous, rendered)
        shrink = len(previous) > len(rendered)
        if not changes and not shrink and cursor == self._painted_cursor:
            return
        parts = [self._paint_begin(), self._cursor_to_live_origin()]
        parked_row = 0
        for index, row in changes:
            if index > parked_row:
                parts.append("\n" * (index - parked_row))
            parts.append(CARRIAGE_RETURN + ERASE_TO_END_OF_LINE + row)
            parked_row = index
        if shrink:
            target = len(rendered)
            if target > parked_row:
                parts.append("\n" * (target - parked_row))
            elif target < parked_row:
                parts.append(f"\x1b[{parked_row - target}A")
            parts.append(CARRIAGE_RETURN + ERASE_BELOW)
            parked_row = target
        if parked_row:
            parts.append(f"\x1b[{parked_row}A")
        parts.append(CARRIAGE_RETURN)
        if cursor is not None:
            row, column = cursor
            if row:
                parts.append(f"\x1b[{row}B")
            parts.append(f"\x1b[{column + 1}G")
        parts.append(self._paint_end(show_cursor=cursor is not None))
        self._emit("".join(parts))
        self._painted = rendered
        self._painted_cursor = cursor
        self._painted_width = self.width

    def enter_alt_screen(self) -> None:
        """Borrow the alternate screen only when its original state is known off."""
        if self._closed or not self._is_tty or self._alt or os.name == "nt" or self._private_modes.get(1049) is not False:
            return
        self._emit(self._cursor_to_live_origin() + ALTERNATE_SCREEN_ON + (HIDE_CURSOR if self._private_modes.get(25) is not None else ""))
        self._alt = True
        self._painted = []
        self._painted_cursor = None
        self._reapply_cursor_style()

    def leave_alt_screen(self) -> None:
        """Return to the entry buffer and force a full repaint of the live region."""
        if not self._alt:
            return
        self._alt = False
        self._painted = []
        # Entering the alternate buffer saved the main-buffer caret at its live origin.
        self._painted_cursor = (0, 0)
        self._resize = True
        cursor = self._private_modes.get(25)
        restore_cursor = SHOW_CURSOR if cursor is True else HIDE_CURSOR if cursor is False else ""
        self._emit(ALTERNATE_SCREEN_OFF + restore_cursor)
        self._reapply_cursor_style()

    def query_default_background_rgb(self) -> tuple[int, int, int] | None:
        """Query OSC 11 once, preserving unrelated bytes read during the timeout."""
        if not self._is_tty or self._input_fd is None:
            colorfgbg = _colorfgbg_rgb()
            self._background_source = "COLORFGBG" if colorfgbg is not None else "unavailable"
            return colorfgbg
        self._emit("\x1b]11;?\x07")
        received = bytearray()
        deadline = time.monotonic() + 0.1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self._read_input_chunk(remaining)
            if chunk is None:
                break
            if not chunk:
                self._eof = True
                break
            received.extend(chunk)
            match = _BACKGROUND_RESPONSE.search(received)
            if match is not None:
                self._pending_input.extend(received[: match.start()])
                self._pending_input.extend(received[match.end() :])
                self._background_source = "osc11"
                return _osc_rgb(match)
        self._pending_input.extend(received)
        colorfgbg = _colorfgbg_rgb()
        self._background_source = "COLORFGBG" if colorfgbg is not None else "unavailable"
        return colorfgbg

    def read_bytes(self, timeout: float | None = None) -> bytes:
        """Read available input bytes, blocking at most ``timeout`` seconds."""
        if self._closed or self._input_fd is None:
            return b""
        if self._pending_input:
            data = bytes(self._pending_input[:_READ_CHUNK])
            del self._pending_input[: len(data)]
            return data
        if self._eof:
            return b""
        data = self._read_input_chunk(timeout)
        if data is None:
            return b""
        if not data:
            self._eof = True
        return data

    def close(self) -> None:
        """Restore the terminal's captured protocol state and original termios."""
        if self._closed:
            return
        self._closed = True
        if self._alt:
            self._alt = False
            self._emit(ALTERNATE_SCREEN_OFF)
            self._painted_cursor = None
            self._reapply_cursor_style()
        if self._is_tty:
            parts = []
            if self._cursor_style_changed and self._original_cursor_style is not None:
                self._set_cursor_style(self._original_cursor_style)
                self._cursor_style_changed = False
            parts.extend((self._paint_begin(), self._cursor_to_live_origin(), ERASE_BELOW, self._paint_end(), RESET_STYLE))
            cursor = self._private_modes.get(25)
            if cursor is not None:
                parts.append(SHOW_CURSOR if cursor else HIDE_CURSOR)
            if 2004 in self._enabled_private_modes:
                parts.append(BRACKETED_PASTE_OFF)
            if self._keyboard_active:
                parts.append(KEYBOARD_POP)
                self._keyboard_active = False
            self._painted = []
            self._painted_cursor = None
            self._emit("".join(parts))
        self._restore_winch()
        self._restore_termination_handlers()
        self._leave_raw_mode()

    # -- internals ---------------------------------------------------------

    def _capture_private_modes(self) -> None:
        requested = (7, 25, 2004, 2026, 1049)
        self._private_modes = dict.fromkeys(requested)
        self._unsupported_private_modes.clear()
        if self._input_fd is None:
            return
        self._emit("".join(f"\x1b[?{mode}$p" for mode in requested))
        received = bytearray()
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline and any(value is None for value in self._private_modes.values()):
            chunk = self._read_input_chunk(max(0.0, deadline - time.monotonic()))
            if chunk is None:
                break
            if not chunk:
                self._eof = True
                break
            received.extend(chunk)
            for match in _PRIVATE_MODE_RESPONSE.finditer(received):
                mode, setting = int(match.group(1)), int(match.group(2))
                if mode in requested:
                    self._private_modes[mode] = setting in (1, 3) if setting in (1, 2, 3, 4) else None
                    if setting == 4:
                        self._unsupported_private_modes.add(mode)
        matched_spans = [match.span() for match in _PRIVATE_MODE_RESPONSE.finditer(received) if int(match.group(1)) in requested]
        cursor = 0
        for start, end in matched_spans:
            self._pending_input.extend(received[cursor:start])
            cursor = end
        self._pending_input.extend(received[cursor:])

    def _frame_row_width(self) -> int:
        if not self._is_tty or self._private_modes.get(7) is not None:
            return self.width
        return max(1, self.width - 1)

    def _paint_begin(self) -> str:
        return "".join(
            (
                HIDE_CURSOR if self._private_modes.get(25) is not None else "",
                SYNC_BEGIN if self._private_modes.get(2026) is False else "",
                AUTOWRAP_OFF if self._private_modes.get(7) is True else "",
            )
        )

    def _paint_end(self, *, show_cursor: bool = True) -> str:
        cursor_mode = self._private_modes.get(25)
        cursor = (SHOW_CURSOR if show_cursor else HIDE_CURSOR) if cursor_mode is not None else ""
        return "".join(
            (
                AUTOWRAP_ON if self._private_modes.get(7) is True else "",
                SYNC_END if self._private_modes.get(2026) is False else "",
                cursor,
            )
        )

    def _cursor_to_live_origin(self) -> str:
        """Return the cached native caret to the owned live-region origin."""
        if self._painted_cursor is None:
            return ""
        row = self._painted_cursor[0]
        return (f"\x1b[{row}A" if row else "") + CARRIAGE_RETURN

    def _enter_application_mode(self) -> None:
        """Enable only session modes whose original state can be restored."""
        paste_mode = self._private_modes.get(2004)
        if os.name == "nt" and paste_mode is not True and (paste_mode is None or 2004 in self._unsupported_private_modes):
            detail = "unsupported" if 2004 in self._unsupported_private_modes else "not reported"
            raise TerminalCapabilityError(f"Windows terminal bracketed paste mode is {detail}; paste-safe input is unavailable")
        parts = []
        if paste_mode is False:
            parts.append(BRACKETED_PASTE_ON)
            self._enabled_private_modes.add(2004)
        parts.extend((KEYBOARD_PROBE, KEYBOARD_ENABLE))
        self._keyboard_active = True
        self._emit("".join(parts))

    def _newline(self) -> str:
        # Raw mode clears OPOST, so committed rows must carry their own CR.
        return "\r\n" if self._raw else "\n"

    def _geometry_stale(self) -> bool:
        stale = self._resize or self._painted_width not in (0, self.width)
        self._resize = False
        return stale

    def _reset_live(self) -> None:
        if self._is_tty:
            self._emit(self._paint_begin() + self._cursor_to_live_origin() + ERASE_BELOW + self._paint_end())
        self._painted = []
        self._painted_cursor = None
        self._painted_width = self.width

    def reset_live(self) -> None:
        """Drop the terminal's live-paint cache; the next frame is emitted in full.

        ``app.display.reset``: a repaint of a corrupted live region. Doesn't touch
        scrollback (only committed rows live there).
        """
        self._reset_live()

    def _emit(self, text: str) -> None:
        with self._lock:
            if self._output_fd in self._windows_console_handles:
                handle = self._windows_console_handles[self._output_fd]
                units = len(text.encode("utf-16-le")) // 2
                written = ctypes.c_ulong()
                ok = self._windows_kernel.WriteConsoleW(
                    handle,
                    ctypes.c_wchar_p(text),
                    units,
                    ctypes.byref(written),
                    None,
                )
                if not ok or written.value != units:
                    self._eof = True
                return
            data = text.encode("utf-8", "replace")
            while data:
                try:
                    written = os.write(self._output_fd, data)
                except BrokenPipeError, OSError:
                    self._eof = True
                    return
                data = data[written:]

    def _enter_raw_mode(self) -> None:
        if self._input_fd is None or not os.isatty(self._input_fd):
            self._is_tty = False
            return
        if os.name == "nt":
            self._enter_windows_console()
            return
        if termios is None or tty is None:
            self._is_tty = False
            return
        try:
            self._saved_termios = termios.tcgetattr(self._input_fd)
            tty.setraw(self._input_fd)
        except termios.error, OSError, ValueError:
            self._saved_termios = None
            self._is_tty = False
            return
        self._raw = True

    def _enter_windows_console(self) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        windows_dll = getattr(ctypes, "Win" + "DLL")
        get_last_error = getattr(ctypes, "get_" + "last_error")
        kernel32 = windows_dll("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WriteConsoleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]

        class _UnicodeChar(ctypes.Union):
            _fields_ = [("UnicodeChar", ctypes.c_uint16), ("AsciiChar", ctypes.c_char)]

        class _KeyEventRecord(ctypes.Structure):
            _fields_ = [
                ("bKeyDown", ctypes.c_int32),
                ("wRepeatCount", ctypes.c_uint16),
                ("wVirtualKeyCode", ctypes.c_uint16),
                ("wVirtualScanCode", ctypes.c_uint16),
                ("uChar", _UnicodeChar),
                ("dwControlKeyState", ctypes.c_uint32),
            ]

        class _InputRecordEvent(ctypes.Union):
            _fields_ = [("KeyEvent", _KeyEventRecord), ("Padding", ctypes.c_uint8 * 16)]

        class _InputRecord(ctypes.Structure):
            _fields_ = [("EventType", ctypes.c_uint16), ("Event", _InputRecordEvent)]

        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReadConsoleInputW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_InputRecord),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.ReadConsoleInputW.restype = wintypes.BOOL
        self._windows_input_record_type = _InputRecord
        self._windows_kernel = kernel32
        try:
            for fd in dict.fromkeys((self._input_fd, self._output_fd)):
                assert fd is not None
                handle = getattr(msvcrt, "get_" + "osfhandle")(fd)
                mode = wintypes.DWORD()
                if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    raise OSError(get_last_error(), "GetConsoleMode failed")
                self._windows_console_handles[fd] = handle
                self._windows_saved_modes[fd] = mode.value
            for fd, original in self._windows_saved_modes.items():
                if fd == self._input_fd:
                    desired = (original & ~(0x0001 | 0x0002 | 0x0004)) | 0x0200
                else:
                    desired = original | 0x0004
                if not kernel32.SetConsoleMode(self._windows_console_handles[fd], desired):
                    raise OSError(get_last_error(), "SetConsoleMode failed")
        except OSError as error:
            try:
                self._leave_windows_console()
            except OSError as restore_error:
                self._is_tty = False
                raise TerminalCapabilityError(f"Windows console mode restore failed: {restore_error}") from restore_error
            self._is_tty = False
            raise TerminalCapabilityError(f"Windows console VT mode unavailable: {error}") from error
        self._raw = True

    def _leave_windows_console(self) -> None:
        if not self._windows_saved_modes:
            return
        errors = []
        remaining: dict[int, int] = {}
        for fd, mode in self._windows_saved_modes.items():
            if not self._windows_kernel.SetConsoleMode(self._windows_console_handles[fd], mode):
                remaining[fd] = mode
                errors.append(OSError(getattr(ctypes, "get_" + "last_error")(), f"SetConsoleMode restore failed for fd {fd}"))
        self._windows_saved_modes = remaining
        self._windows_console_handles = {fd: self._windows_console_handles[fd] for fd in remaining}
        self._raw = bool(remaining)
        if errors:
            raise errors[0]

    def _leave_raw_mode(self) -> None:
        if os.name == "nt":
            self._leave_windows_console()
            return
        if self._saved_termios is None or self._input_fd is None or termios is None:
            return
        try:
            termios.tcsetattr(self._input_fd, termios.TCSADRAIN, self._saved_termios)
        except termios.error, OSError, ValueError:
            pass
        finally:
            self._saved_termios = None
            self._raw = False

    def _install_winch(self) -> None:
        try:
            self._previous_winch = signal.signal(signal.SIGWINCH, self._on_winch)
        except ValueError, OSError, AttributeError:
            self._previous_winch = None

    def _restore_winch(self) -> None:
        if self._previous_winch is None:
            return
        try:
            signal.signal(signal.SIGWINCH, self._previous_winch)
        except ValueError, OSError:
            pass
        self._previous_winch = None

    def _install_termination_handlers(self) -> None:
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                self._previous_termination[signum] = signal.signal(signum, self._on_termination)
            except ValueError, OSError, AttributeError:
                continue
        suspend = getattr(signal, "SIGTSTP", None)
        if suspend is not None:
            try:
                self._previous_suspend = signal.signal(suspend, self._on_suspend)
            except ValueError, OSError, AttributeError:
                self._previous_suspend = None

    def _restore_termination_handlers(self) -> None:
        for signum, previous in self._previous_termination.items():
            try:
                signal.signal(signum, previous)
            except ValueError, OSError:
                continue
        self._previous_termination.clear()
        if self._previous_suspend is not None:
            try:
                signal.signal(signal.SIGTSTP, self._previous_suspend)
            except ValueError, OSError, AttributeError:
                pass
            self._previous_suspend = None

    def _on_termination(self, signum: int, frame: Any) -> None:
        previous = self._previous_termination.get(signum, signal.SIG_DFL)
        self.close()
        if previous is signal.SIG_IGN:
            return
        if callable(previous):
            previous(signum, frame)
            return
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _on_suspend(self, _signum: int, _frame: Any) -> None:
        self._restore_for_suspend()
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)
        signal.signal(signal.SIGTSTP, self._on_suspend)
        self._enter_raw_mode()
        self._capture_private_modes()
        self._enter_application_mode()
        if self._cursor_style_changed:
            self._reapply_cursor_style()
        self._resize = True
        if self._suspended_alt:
            self._suspended_alt = False
            self.enter_alt_screen()

    def _restore_for_suspend(self) -> None:
        self._suspended_alt = self._alt
        if self._alt:
            self._alt = False
            self._emit(ALTERNATE_SCREEN_OFF)
            self._painted_cursor = None
        if self._is_tty:
            parts = []
            if self._cursor_style_changed and self._original_cursor_style is not None:
                parts.append(f"\x1b[{self._original_cursor_style} q")
            parts.extend((self._paint_begin(), self._cursor_to_live_origin(), ERASE_BELOW, self._paint_end(), RESET_STYLE))
            if 2004 in self._enabled_private_modes:
                parts.append(BRACKETED_PASTE_OFF)
            if self._keyboard_active:
                parts.append(KEYBOARD_POP)
            cursor = self._private_modes.get(25)
            if cursor is not None:
                parts.append(SHOW_CURSOR if cursor else HIDE_CURSOR)
            self._emit("".join(parts))
        self._painted = []
        self._painted_cursor = None
        self._enabled_private_modes.clear()
        self._keyboard_active = False
        self._leave_raw_mode()

    def _on_winch(self, _signum: int, _frame: Any) -> None:
        self._refresh_size()
        self._resize = True

    def _refresh_size(self) -> None:
        size = _detect_size(self._output_fd)
        if size != self._size:
            self._size = size
            self._resize = True


#: Max raw bytes base64-encoded into one OSC 52 sequence (Codex parity).
OSC52_MAX_RAW_BYTES = 100_000


def copy_to_clipboard(text: str, *, tmux: bool | None = None) -> None:
    """Write ``text`` to the system clipboard via OSC 52 (Codex ``osc52_copy`` parity).

    Emitted on stdout so it reaches the terminal emulator even over SSH; the
    tmux DCS wrapper is applied inside tmux (auto-detected via ``$TMUX``).
    Raises ``ValueError`` when the payload exceeds ``OSC52_MAX_RAW_BYTES`` and
    ``OSError`` when stdout is unavailable.
    """
    import base64

    raw = text.encode("utf-8")
    if len(raw) > OSC52_MAX_RAW_BYTES:
        raise ValueError(f"clipboard payload exceeds {OSC52_MAX_RAW_BYTES} bytes")
    payload = base64.b64encode(raw).decode("ascii")
    sequence = f"\x1b]52;c;{payload}\x07"
    inside_tmux = os.environ.get("TMUX") is not None if tmux is None else tmux
    if inside_tmux:
        sequence = f"\x1bPtmux;\x1b{sequence}\x1b\\"
    sys.stdout.write(sequence)
    sys.stdout.flush()
