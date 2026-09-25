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
* The alternate screen is never borrowed here.

The live region is always anchored at the parked cursor: after every
``paint_frame`` the cursor sits at column 1 of the region's first row, and after
every ``commit_rows`` it sits at column 1 of the first row *below* the committed
ones -- which is where the live region then starts. Committed rows are written
top-down and the terminal scrolls when they overflow, pushing only already
committed rows into scrollback (the live region is erased first, so an
unfinished frame can never be pushed).

When stdout is not a tty (or raw mode cannot be entered) the terminal degrades
to a plain append-only log: frames are re-emitted only when their content
changes, and no escape sequences are written.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import threading
import tty
from collections.abc import Iterator, Sequence
from io import StringIO
from typing import Any

from rich.cells import cell_len, chop_cells
from rich.color import ColorSystem
from rich.console import Console, OverflowMethod, RenderableType
from rich.theme import Theme as RichTheme

from .region import diff_rows
from .theme import color_system_name

__all__ = [
    "PAINT_BEGIN",
    "PAINT_END",
    "SHOW_CURSOR",
    "Terminal",
    "clamp_row",
    "format_number",
    "render_lines",
    "visible_width",
    "wrap_row",
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

#: Alternate screen buffer borrow. Only a fullscreen overlay uses it (the session
#: picker); the transcript itself never leaves the normal buffer, so terminal
#: scrollback survives (see ``.omo/plans/tui-omp-research.md`` §1.1).
ALTERNATE_SCREEN_ON = "\x1b[?1049h"
ALTERNATE_SCREEN_OFF = "\x1b[?1049l"

# Session-scoped terminal protocols (omp ``terminal.ts``; see
# ``.omo/plans/tui-input-spec.md`` §8/§9.2):
#   bracketed paste      enable ``\x1b[?2004h`` / disable ``\x1b[?2004l``
#   keyboard enhancement probe ``\x1b[?u\x1b[c``, push ``\x1b[>5u`` (disambiguate
#                        + report base layout, no event reporting), pop ``\x1b[<u``
# The probe is fire-and-forget: a terminal that ignores it keeps working, and the
# renderer never needs a cursor-position report. Replies arrive as input bytes and
# belong to the input layer (it must consume the DA1 / ``CSI ? u`` replies).
BRACKETED_PASTE_ON = "\x1b[?2004h"
BRACKETED_PASTE_OFF = "\x1b[?2004l"
KEYBOARD_PROBE = "\x1b[?u\x1b[c"
KEYBOARD_ENABLE = "\x1b[>5u"
KEYBOARD_POP = "\x1b[<u"

#: Frame opener: hide the cursor, open synchronized output, disable autowrap.
PAINT_BEGIN = HIDE_CURSOR + SYNC_BEGIN + AUTOWRAP_OFF
#: Frame closer: restore autowrap, close synchronized output.
PAINT_END = AUTOWRAP_ON + SYNC_END

_DEFAULT_SIZE = (80, 24)
_READ_CHUNK = 4096
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


def _prefix_cells(text: str, width: int) -> str:
    """Longest prefix of ``text`` whose display width does not exceed ``width``."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    taken: list[str] = []
    used = 0
    for chunk in chop_cells(text, width):
        size = cell_len(chunk)
        if used + size > width:
            break
        taken.append(chunk)
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

    Prose composed by the transcript is wrapped by rich itself (markdown, code
    blocks); this helper exists for rows that are already styled, where the
    wrapping must not slice an escape sequence in half.
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
    if term in ("", "dumb"):
        return None
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return ColorSystem.TRUECOLOR
    if "256" in term:
        return ColorSystem.EIGHT_BIT
    return ColorSystem.STANDARD


def _detect_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


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
        self._lock = threading.Lock()
        self._painted: list[str] = []
        self._painted_width = 0
        self._resize = False
        self._alt = False
        self._raw = False
        self._closed = False
        self._eof = False
        self._saved_termios: list[Any] | None = None
        self._previous_winch: Any = None

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls) -> Terminal:
        """Open the process terminal: raw mode, no alternate screen.

        Degrades to a non-tty log when stdout is not a terminal, when raw mode
        is unavailable, or when output has no colour support.
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
        if output_is_tty:
            terminal._enter_raw_mode()
        if terminal._is_tty:
            terminal._enter_application_mode()
            terminal._install_winch()
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
    def has_input(self) -> bool:
        """Whether an input descriptor exists to read from at all.

        ``False`` when stdin has no file descriptor (closed, or a stream without
        one): reads can then never return data, and a caller polling
        :meth:`read_bytes` would spin.
        """
        return self._input_fd is not None and not self._eof

    @property
    def alt_screen(self) -> bool:
        """Whether the alternate screen buffer is currently borrowed."""
        return self._alt

    def resize_pending(self) -> bool:
        """Whether a SIGWINCH arrived since the last frame was written.

        The flag is cleared by the next :meth:`paint_frame` or
        :meth:`commit_rows`, which repaints from scratch at the new size.
        """
        return self._resize

    # -- writes ------------------------------------------------------------

    def write(self, text: str) -> None:
        """Write raw text with no frame brackets (single-writer, atomic call)."""
        if self._closed or not text:
            return
        self._emit(text)

    def commit_rows(self, rows: Sequence[str]) -> None:
        """Write finalized rows into the native scrollback, exactly once.

        Rows are clamped to the terminal width, written top-down, and never
        rewritten. The live region is erased first so the scroll a full screen
        triggers can only push committed rows.
        """
        if self._closed:
            return
        rows = list(rows)
        if not rows:
            return
        if not self._is_tty:
            self._emit("".join(row + "\n" for row in (clamp_row(row, self.width) for row in rows)))
            return
        if self._geometry_stale():
            self._painted = []
        parts = [PAINT_BEGIN]
        if self._painted:
            # Parked at the region top: erase the unfinished frame before the
            # commit can scroll the screen.
            parts.append(CARRIAGE_RETURN + ERASE_BELOW)
            self._painted = []
        for row in rows:
            parts.append(clamp_row(row, self.width))
            parts.append(self._newline())
        parts.append(PAINT_END)
        self._emit("".join(parts))

    def paint_frame(self, rows: Sequence[str]) -> None:
        """Repaint the live region, rewriting only rows that changed.

        ``rows`` is the complete desired live frame; the bottom ``height`` rows
        win when it is taller than the terminal (the ledger is expected to flush
        the older overflow first). Identical frames emit no bytes.
        """
        if self._closed:
            return
        if self._geometry_stale():
            self._reset_live()
        if not self._is_tty:
            frame = [clamp_row(row, self.width) for row in rows]
            frame = frame[-self.height :]
            if frame == self._painted:
                return
            self._painted = frame
            self._emit("".join(row + "\n" for row in frame))
            return

        frame = [clamp_row(row, self.width) for row in rows]
        frame = frame[-self.height :]
        previous = self._painted
        changes = diff_rows(previous, frame)
        shrink = len(previous) > len(frame)
        if not changes and not shrink:
            return

        parts = [PAINT_BEGIN]
        cursor = 0
        for index, row in changes:
            if index > cursor:
                parts.append("\n" * (index - cursor))
            parts.append(CARRIAGE_RETURN + ERASE_TO_END_OF_LINE + row)
            cursor = index
        if shrink:
            target = len(frame)
            if target > cursor:
                parts.append("\n" * (target - cursor))
            elif target < cursor:
                parts.append(f"\x1b[{cursor - target}A")
            parts.append(CARRIAGE_RETURN + ERASE_BELOW)
            cursor = target
        if cursor:
            parts.append(f"\x1b[{cursor}A")
        parts.append(CARRIAGE_RETURN)
        parts.append(PAINT_END)
        self._emit("".join(parts))
        self._painted = frame
        self._painted_width = self.width

    def enter_alt_screen(self) -> None:
        """Borrow the terminal's alternate screen buffer (``\\x1b[?1049h``).

        Only a fullscreen overlay does this; the caller erases the live region
        first so its rows cannot leak into the borrowed, empty buffer.
        """
        if self._closed or not self._is_tty or self._alt:
            return
        self._alt = True
        self._painted = []
        self._emit(ALTERNATE_SCREEN_ON + HIDE_CURSOR)

    def leave_alt_screen(self) -> None:
        """Return to the normal buffer and force a full repaint of the live region.

        The normal buffer's live region was erased before the borrow, so the
        geometry counts as stale: the next frame repaints from scratch.
        """
        if not self._alt:
            return
        self._alt = False
        self._painted = []
        self._resize = True
        self._emit(ALTERNATE_SCREEN_OFF + SHOW_CURSOR)

    def read_bytes(self, timeout: float | None = None) -> bytes:
        """Read available input bytes, blocking at most ``timeout`` seconds.

        Returns ``b""`` on timeout, when the input is not a terminal, or after
        end of file.
        """
        if self._closed or self._eof or self._input_fd is None:
            return b""
        try:
            ready, _, _ = select.select([self._input_fd], [], [], timeout)
        except OSError, ValueError:
            self._eof = True
            return b""
        if not ready:
            return b""
        try:
            data = os.read(self._input_fd, _READ_CHUNK)
        except OSError:
            self._eof = True
            return b""
        if not data:
            self._eof = True
        return data

    def close(self) -> None:
        """Restore the terminal: erase the live region, unhide, restore termios."""
        if self._closed:
            return
        self._closed = True
        if self._alt:
            # Leaving the alternate buffer also restores the normal screen; do it
            # before the protocol teardown so nothing else lands in the alt buffer.
            self._alt = False
            self._emit(ALTERNATE_SCREEN_OFF)
        if self._is_tty:
            # Erase the live region inside one balanced paint bracket (the parked
            # cursor sits below every committed row, so nothing durable is
            # touched), then leave the session protocols and show the cursor.
            parts = [PAINT_BEGIN, CARRIAGE_RETURN, ERASE_BELOW, PAINT_END]
            self._painted = []
            parts.extend(
                [
                    BRACKETED_PASTE_OFF,
                    KEYBOARD_POP,
                    RESET_STYLE,
                    SHOW_CURSOR,
                ]
            )
            self._emit("".join(parts))
        self._restore_winch()
        self._leave_raw_mode()

    # -- internals ---------------------------------------------------------

    def _enter_application_mode(self) -> None:
        """Enable session-scoped input protocols (bracketed paste + keys)."""
        self._emit(BRACKETED_PASTE_ON + KEYBOARD_PROBE + KEYBOARD_ENABLE)

    def _newline(self) -> str:
        # Raw mode clears OPOST, so committed rows must carry their own CR.
        return "\r\n" if self._raw else "\n"

    def _geometry_stale(self) -> bool:
        stale = self._resize or self._painted_width not in (0, self.width)
        self._resize = False
        return stale

    def _reset_live(self) -> None:
        self._painted = []
        self._painted_width = self.width
        if self._is_tty:
            self._emit(PAINT_BEGIN + CARRIAGE_RETURN + ERASE_BELOW + PAINT_END)

    def _emit(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        with self._lock:
            while data:
                try:
                    written = os.write(self._output_fd, data)
                except BrokenPipeError:
                    self._eof = True
                    return
                except OSError:
                    return
                data = data[written:]

    def _enter_raw_mode(self) -> None:
        if self._input_fd is None or not os.isatty(self._input_fd):
            return
        try:
            self._saved_termios = termios.tcgetattr(self._input_fd)
            tty.setraw(self._input_fd)
        except termios.error, OSError, ValueError:
            self._saved_termios = None
            self._is_tty = False
            return
        self._raw = True

    def _leave_raw_mode(self) -> None:
        if self._saved_termios is None or self._input_fd is None:
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

    def _on_winch(self, _signum: int, _frame: Any) -> None:
        self._refresh_size()
        self._resize = True

    def _refresh_size(self) -> None:
        size = _detect_size(self._output_fd)
        if size != self._size:
            self._size = size
