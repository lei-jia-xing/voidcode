"""Raw-mode, private-mode capture, and process-signal lifecycle for the terminal."""

from __future__ import annotations

import os
import signal
import time
from typing import Any

import regex

from .term_ansi import (
    ALTERNATE_SCREEN_OFF,
    BRACKETED_PASTE_OFF,
    BRACKETED_PASTE_ON,
    ERASE_BELOW,
    HIDE_CURSOR,
    KEYBOARD_ENABLE,
    KEYBOARD_POP,
    KEYBOARD_PROBE,
    RESET_STYLE,
    SHOW_CURSOR,
)
from .term_capabilities import TerminalCapabilityError, _detect_size, termios, tty

__all__ = ["TerminalSessionControl"]

_PRIVATE_MODE_RESPONSE = regex.compile(rb"\x1b\[\?(\d+);([0-4])\$y")


class TerminalSessionControl:
    """Terminal mixin owning termios, private modes, and signal handlers."""

    _is_tty: bool
    _input_fd: int | None
    _output_fd: int
    _raw: bool
    _saved_termios: list[Any] | None
    _eof: bool
    _size: tuple[int, int]
    _resize: bool
    _alt: bool
    _suspended_alt: bool
    _painted: list[str]
    _painted_cursor: tuple[int, int] | None
    _keyboard_active: bool
    _enabled_private_modes: set[int]
    _unsupported_private_modes: set[int]
    _private_modes: dict[int, bool | None]
    _pending_input: bytearray
    _previous_winch: Any
    _previous_termination: dict[int, Any]
    _previous_suspend: Any

    def _emit(self, text: str) -> None:
        raise NotImplementedError

    def _read_input_chunk(self, timeout: float | None) -> bytes | None:
        raise NotImplementedError

    def _paint_begin(self) -> str:
        raise NotImplementedError

    def _cursor_to_live_origin(self) -> str:
        raise NotImplementedError

    def _paint_end(self, *, show_cursor: bool = True) -> str:
        raise NotImplementedError

    def enter_alt_screen(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

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
