"""Terminal I/O, synchronized frame rendering, and ANSI layout for the TUI."""

from __future__ import annotations

import importlib
import os
import secrets
import select
import signal
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from io import StringIO
from typing import Any

import regex
from rich.cells import cell_len
from rich.color import ColorSystem
from rich.console import Console, OverflowMethod, RenderableType
from rich.theme import Theme as RichTheme

from .kitty_graphics import (
    KITTY_PROBE_ID,
    delete_image,
    delete_placement,
    parse_reply,
    place_image,
    probe_sequence,
    transmit_png,
)
from .region import KittyImagePlacement, TuiFrame, diff_rows
from .styles import color_system_name

termios: Any = importlib.import_module("termios") if os.name != "nt" else None
tty: Any = importlib.import_module("tty") if os.name != "nt" else None

__all__ = [
    "TerminalCapabilityError",
    "TerminalKittyReply",
    "Terminal",
    "copy_to_clipboard",
    "crop_row_right",
    "escape_terminal_controls",
    "format_number",
    "render_lines",
    "visible_width",
    "wrap_plain",
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
_PRIVATE_MODE_RESPONSE = regex.compile(rb"\x1b\[\?(\d+);([0-4])\$y")


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


def _detect_cell_pixel_size(fd: int | None) -> tuple[float, float] | None:
    if fd is None or os.name == "nt" or termios is None or not hasattr(termios, "TIOCGWINSZ"):
        return None
    try:
        import array
        import fcntl

        size = array.array("H", [0, 0, 0, 0])
        fcntl.ioctl(fd, termios.TIOCGWINSZ, size, True)
    except ImportError, OSError, ValueError:
        return None
    rows, columns, pixel_width, pixel_height = size
    if min(rows, columns, pixel_width, pixel_height) <= 0:
        return None
    return pixel_width / columns, pixel_height / rows


def _detect_color_system(is_tty: bool) -> ColorSystem | None:
    # no-color.org: presence, not truthiness, disables color (empty included).
    if "NO_COLOR" in os.environ:
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


class TerminalCapabilityError(RuntimeError):
    """The requested native terminal behavior cannot be queried safely."""


@dataclass(frozen=True, slots=True)
class TerminalKittyReply:
    sha256: str
    operation: str
    message: str


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
    ) -> None:
        self._input_fd = input_fd
        self._output_fd = output_fd
        self._is_tty = is_tty
        self._size = size
        self._color_system = color_system
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
        self._suspended_alt = False
        self._pending_input = bytearray()
        self._kitty_supported = False
        self._kitty_probe_pending = False
        self._kitty_probed = False
        self._kitty_input = bytearray()
        self._kitty_replies: list[TerminalKittyReply] = []
        self._kitty_uploads: dict[int, tuple[str, tuple[int, int]]] = {}
        self._kitty_pending_placements: dict[tuple[int, int], str] = {}
        self._kitty_images: dict[str, tuple[int, tuple[int, int]]] = {}
        self._kitty_image_ids: dict[int, str] = {}
        self._kitty_placements: dict[str, int] = {}
        self._painted_image: KittyImagePlacement | None = None
        self._keyboard_active = False
        self._enabled_private_modes: set[int] = set()
        self._unsupported_private_modes: set[int] = set()

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
    def content_width(self) -> int:
        """Widest row that ``commit_rows``/``paint_frame`` will accept unwrapped."""
        return self._frame_row_width()

    @property
    def height(self) -> int:
        """Current terminal height in rows."""
        return self._size[1]

    @property
    def is_tty(self) -> bool:
        """Whether frames are written as escape sequences to a real terminal."""
        return self._is_tty

    @property
    def kitty_graphics_supported(self) -> bool:
        self._ensure_kitty_probed()
        return self._kitty_supported

    def _ensure_kitty_probed(self) -> None:
        """Run the one graphics-capability probe, on first image render instead of at startup."""
        if self._kitty_probed:
            return
        with self._lock:
            if self._kitty_probed:
                return
            self._kitty_probed = True
            self._probe_kitty_graphics()

    @property
    def cell_pixel_size(self) -> tuple[float, float] | None:
        return _detect_cell_pixel_size(self._output_fd) if self._is_tty else None

    def cached_kitty_image(self, sha256: str) -> tuple[int, int] | None:
        cached = self._kitty_images.get(sha256)
        return cached[1] if cached is not None else None

    def kitty_placement_id(self, sha256: str) -> int | None:
        if sha256 not in self._kitty_images:
            return None
        placement = self._kitty_placements.get(sha256)
        if placement is None:
            placement = self._new_kitty_id()
            self._kitty_placements[sha256] = placement
        return placement

    def upload_kitty_png(self, sha256: str, png: bytes, dimensions: tuple[int, int]) -> int:
        """Transmit trusted PNG bytes once; the correlated terminal reply validates decoding."""
        self._ensure_kitty_probed()
        if not self._kitty_supported:
            raise TerminalCapabilityError("Kitty Graphics is not supported by the active terminal")
        cached = self._kitty_images.get(sha256)
        if cached is not None:
            return cached[0]
        for image_id, (pending_sha, _) in self._kitty_uploads.items():
            if pending_sha == sha256:
                return image_id
        image_id = self._new_kitty_id()
        self._kitty_uploads[image_id] = (sha256, dimensions)
        self._kitty_image_ids[image_id] = sha256
        try:
            with self._lock:
                for sequence in transmit_png(image_id, png):
                    self._emit(sequence.decode("ascii"))
        except BaseException:
            self._kitty_uploads.pop(image_id, None)
            self._kitty_image_ids.pop(image_id, None)
            raise
        if self._eof:
            self._kitty_uploads.pop(image_id, None)
            self._kitty_image_ids.pop(image_id, None)
            raise TerminalCapabilityError("terminal disconnected during Kitty image upload")
        return image_id

    def drain_kitty_replies(self) -> tuple[TerminalKittyReply, ...]:
        replies = tuple(self._kitty_replies)
        self._kitty_replies.clear()
        return replies

    @property
    def color_system(self) -> ColorSystem | None:
        """Detected colour depth; ``None`` means "no colour"."""
        return self._color_system

    @property
    def eof(self) -> bool:
        """Whether the input side reported end of file."""
        return self._eof

    @property
    def has_input(self) -> bool:
        """Whether an input descriptor exists to read from at all."""
        return self._input_fd is not None and not self._eof

    def _read_input_chunk(self, timeout: float | None) -> bytes | None:
        if self._input_fd is None:
            return b""
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
        parts = [self._paint_begin(), self._cursor_to_live_origin(), self._forget_painted_image()]
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
        """Paint one typed frame and place its optional native cursor/image."""
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
        image = frame.image
        if image is not None:
            if split <= image.row < split + len(rendered) and self._kitty_supported and image.sha256 in self._kitty_images:
                image = KittyImagePlacement(image.sha256, image.row - split, image.column, image.columns, image.placement_id)
            else:
                image = None
        previous = self._painted
        changes = diff_rows(previous, rendered)
        shrink = len(previous) > len(rendered)
        graphics_changed = image != self._painted_image
        if not changes and not shrink and cursor == self._painted_cursor and not graphics_changed:
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
        if graphics_changed:
            if parked_row:
                parts.append(f"\x1b[{parked_row}A")
            parts.append(CARRIAGE_RETURN)
            parked_row = 0
            parts.append(self._forget_painted_image())
            if image is not None:
                image_id = self._kitty_images[image.sha256][0]
                self._kitty_pending_placements[(image_id, image.placement_id)] = image.sha256
                if image.row:
                    parts.append(f"\x1b[{image.row}B")
                if image.column:
                    parts.append(f"\x1b[{image.column + 1}G")
                parts.append(place_image(image_id, image.placement_id, image.columns).decode("ascii"))
                if image.row:
                    parts.append(f"\x1b[{image.row}A")
                parts.append(CARRIAGE_RETURN)
            self._painted_image = image
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
        self._emit(
            self._forget_painted_image()
            + self._cursor_to_live_origin()
            + ALTERNATE_SCREEN_ON
            + (HIDE_CURSOR if self._private_modes.get(25) is not None else "")
        )
        self._alt = True
        self._painted = []
        self._painted_cursor = None

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
        self._emit(self._forget_painted_image() + ALTERNATE_SCREEN_OFF + restore_cursor)

    def read_bytes(self, timeout: float | None = None) -> bytes:
        """Read available input while routing only correlated Kitty replies away."""
        if self._closed or self._input_fd is None:
            return b""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            if self._pending_input:
                data = bytes(self._pending_input[:_READ_CHUNK])
                del self._pending_input[: len(data)]
                return data
            if self._eof:
                return b""
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            data = self._read_input_chunk(remaining)
            if data is None:
                if self._kitty_input in (b"\x1b", b"\x1b_"):
                    incomplete = bytes(self._kitty_input)
                    self._kitty_input.clear()
                    return incomplete
                return b""
            if not data:
                self._eof = True
                return b""
            passthrough = self._filter_kitty_replies(data)
            if passthrough:
                self._pending_input.extend(passthrough[_READ_CHUNK:])
                return passthrough[:_READ_CHUNK]
            if deadline is not None and time.monotonic() >= deadline:
                return b""

    def close(self) -> None:
        """Restore the terminal's captured protocol state and original termios."""
        if self._closed:
            return
        self._closed = True
        if self._alt:
            self._alt = False
            self._emit(ALTERNATE_SCREEN_OFF)
            self._painted_cursor = None
        if self._is_tty:
            parts = []
            cleanup = "".join(delete_image(image_id).decode("ascii") for image_id in self._kitty_image_ids)
            parts.extend((self._paint_begin(), self._cursor_to_live_origin(), cleanup, ERASE_BELOW, self._paint_end(), RESET_STYLE))
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
            self._kitty_images.clear()
            self._kitty_image_ids.clear()
            self._kitty_uploads.clear()
            self._kitty_pending_placements.clear()
            self._kitty_placements.clear()
            self._painted_image = None
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

    def _probe_kitty_graphics(self) -> None:
        if not self._is_tty or self._input_fd is None:
            return
        self._kitty_probe_pending = True
        self._emit(probe_sequence().decode("ascii"))
        deadline = time.monotonic() + 0.12
        while self._kitty_probe_pending and time.monotonic() < deadline:
            chunk = self._read_input_chunk(max(0.0, deadline - time.monotonic()))
            if chunk is None:
                break
            if not chunk:
                self._eof = True
                break
            self._pending_input.extend(self._filter_kitty_replies(chunk))

    def _filter_kitty_replies(self, data: bytes) -> bytes:
        self._kitty_input.extend(data)
        passthrough = bytearray()
        marker = b"\x1b_G"
        while self._kitty_input:
            start = self._kitty_input.find(marker)
            if start < 0:
                keep = 0
                for length in range(1, len(marker)):
                    if self._kitty_input.endswith(marker[:length]):
                        keep = length
                passthrough.extend(self._kitty_input[:-keep] if keep else self._kitty_input)
                if keep:
                    del self._kitty_input[:-keep]
                else:
                    self._kitty_input.clear()
                break
            if start:
                passthrough.extend(self._kitty_input[:start])
                del self._kitty_input[:start]
            end = self._kitty_input.find(b"\x1b\\", len(marker))
            if end < 0:
                break
            sequence = bytes(self._kitty_input[: end + 2])
            del self._kitty_input[: end + 2]
            reply = parse_reply(sequence)
            if reply is None or not self._route_kitty_reply(reply):
                passthrough.extend(sequence)
        return bytes(passthrough)

    def _route_kitty_reply(self, reply: Any) -> bool:
        image_id = reply.image_id
        if image_id == KITTY_PROBE_ID and self._kitty_probe_pending:
            self._kitty_supported = reply.message == "OK"
            self._kitty_probe_pending = False
            return True
        if image_id is None:
            return False
        upload = self._kitty_uploads.pop(image_id, None)
        if upload is not None:
            sha256, dimensions = upload
            if reply.message == "OK":
                self._kitty_images[sha256] = (image_id, dimensions)
                self._kitty_replies.append(TerminalKittyReply(sha256, "upload", reply.message))
            else:
                self._kitty_image_ids.pop(image_id, None)
                self._kitty_replies.append(TerminalKittyReply(sha256, "upload", reply.message))
            return True
        placement_id = reply.placement_id
        if placement_id is not None:
            sha256 = self._kitty_pending_placements.pop((image_id, placement_id), None)
            if sha256 is not None:
                if reply.message != "OK":
                    self._kitty_images.pop(sha256, None)
                    self._kitty_image_ids.pop(image_id, None)
                self._kitty_replies.append(TerminalKittyReply(sha256, "placement", reply.message))
                return True
        return image_id in self._kitty_image_ids

    def _new_kitty_id(self) -> int:
        used = set(self._kitty_image_ids) | set(self._kitty_placements.values()) | {KITTY_PROBE_ID}
        while True:
            value = secrets.randbelow(0xFFFFFFFF - 1) + 1
            if value not in used:
                return value

    def _forget_painted_image(self) -> str:
        image = self._painted_image
        self._painted_image = None
        if image is None:
            return ""
        cached = self._kitty_images.get(image.sha256)
        if cached is None:
            return ""
        image_id = cached[0]
        self._kitty_pending_placements[(image_id, image.placement_id)] = image.sha256
        return delete_placement(image_id, image.placement_id).decode("ascii")

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
            self._emit(self._paint_begin() + self._cursor_to_live_origin() + self._forget_painted_image() + ERASE_BELOW + self._paint_end())
        self._painted = []
        self._painted_cursor = None
        self._painted_width = self.width

    def _emit(self, text: str) -> None:
        with self._lock:
            data = text.encode("utf-8", "replace")
            while data:
                try:
                    written = os.write(self._output_fd, data)
                except BrokenPipeError, OSError:
                    self._eof = True
                    return
                data = data[written:]

    def _enter_raw_mode(self) -> None:
        if os.name == "nt":
            raise TerminalCapabilityError("interactive terminal requires a VT-capable terminal; native Windows console support was removed")
        if self._input_fd is None or not os.isatty(self._input_fd):
            self._is_tty = False
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

    def _leave_raw_mode(self) -> None:
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
