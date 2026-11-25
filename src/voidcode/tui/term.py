"""Terminal I/O, synchronized frame rendering, and ANSI layout for the TUI.

The concerns live behind this facade:

- :mod:`term_text` — pure cell metrics, wrapping, and row layout;
- :mod:`term_capabilities` — size/colour/cell detection and capability errors;
- :mod:`term_session` — raw mode, private modes, and signal lifecycle;
- :mod:`term_paint` — committed rows and the inline live region;
- :mod:`term_kitty` — Kitty Graphics probing, uploads, and replies;
- :mod:`term_clipboard` — OSC 52 clipboard text.
"""

from __future__ import annotations

import os
import select
import sys
import threading
import time
from typing import Any

from rich.cells import cell_len as cell_len
from rich.color import ColorSystem

from .kitty_graphics import delete_image
from .term_ansi import (
    ALTERNATE_SCREEN_OFF,
    BRACKETED_PASTE_OFF,
    ERASE_BELOW,
    HIDE_CURSOR,
    KEYBOARD_POP,
    RESET_STYLE,
    SHOW_CURSOR,
)
from .term_capabilities import (
    _DEFAULT_SIZE,
    TerminalCapabilityError,
    _detect_cell_pixel_size,
    _detect_color_system,
    _detect_size,
    _fileno,
)
from .term_clipboard import OSC52_MAX_RAW_BYTES as OSC52_MAX_RAW_BYTES
from .term_clipboard import copy_to_clipboard
from .term_kitty import KittyGraphicsControl, TerminalKittyReply
from .term_paint import LiveRegionPainting
from .term_session import TerminalSessionControl
from .term_text import clamp_row as clamp_row
from .term_text import (
    crop_row_right,
    escape_terminal_controls,
    format_number,
    grapheme_spans,
    render_lines,
    visible_width,
    wrap_plain,
    wrap_row,
)

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

_READ_CHUNK = 4096


class Terminal(KittyGraphicsControl, LiveRegionPainting, TerminalSessionControl):
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
        self._painted_image = None
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
    def cell_pixel_size(self) -> tuple[float, float] | None:
        return _detect_cell_pixel_size(self._output_fd) if self._is_tty else None

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
