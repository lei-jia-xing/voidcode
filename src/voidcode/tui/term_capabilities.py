"""Terminal capability detection: file descriptors, window size, cells, colour."""

from __future__ import annotations

import importlib
import os
from typing import Any

from rich.color import ColorSystem

__all__ = [
    "TerminalCapabilityError",
    "_DEFAULT_SIZE",
    "_detect_cell_pixel_size",
    "_detect_color_system",
    "_detect_size",
    "_fileno",
]

termios: Any = importlib.import_module("termios") if os.name != "nt" else None
tty: Any = importlib.import_module("tty") if os.name != "nt" else None

_DEFAULT_SIZE = (80, 24)


class TerminalCapabilityError(RuntimeError):
    """The requested native terminal behavior cannot be queried safely."""


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
