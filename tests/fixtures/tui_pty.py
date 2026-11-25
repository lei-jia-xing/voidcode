"""Real PTY process and ANSI screen model for inline-TUI acceptance tests.

The model is deliberately small: it interprets the control sequences the TUI
writer emits (carriage return, line feed, relative cursor moves, column moves,
erase-in-line, erase-below, SGR, private modes and DECRQM queries) and treats
everything else as visible text. Width changes clamp future writes but never
reflow existing rows, so history stays inspectable and tests do not assume a
terminal-specific reflow policy. Non-ASCII cell widths are not modelled; tests
using this model keep their text ASCII.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


class AnsiScreen:
    """Append-only document plus the visible terminal window over its tail."""

    _TOKEN = re.compile(
        r"(?P<esc>\x1b\[(?P<params>[0-?]*)(?P<inter>[ -/]*)(?P<final>[@-~]))"
        r"|(?P<cr>\r)"
        r"|(?P<lf>\n)"
        r"|(?P<text>[^\x1b\r\n]+)"
        r"|(?P<other>\x1b.)",
        re.DOTALL,
    )

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.lines: list[str] = [""]
        self.row = 0
        self.col = 0

    def feed(self, chunk: bytes) -> None:
        for match in self._TOKEN.finditer(chunk.decode("utf-8", errors="replace")):
            kind = match.lastgroup
            if kind == "text":
                self._put(match.group())
            elif kind == "cr":
                self.col = 0
            elif kind == "lf":
                self._newline()
            elif kind == "esc":
                self._escape(match.group("params"), match.group("final"))

    def resize(self, width: int, height: int) -> None:
        """Adopt a new window size; existing rows keep their historical text."""
        self.width = max(1, width)
        self.height = max(1, height)

    @property
    def document(self) -> str:
        """Every row ever painted, including rows now above the viewport."""
        return "\n".join(self.lines)

    def visible(self) -> str:
        """Rows inside the current window, oldest first."""
        return "\n".join(self.lines[-self.height :])

    @property
    def cursor_cell(self) -> tuple[int, int]:
        """Zero-based cursor cell relative to the visible window's top-left."""
        first = max(0, len(self.lines) - self.height)
        return self.row - first, self.col

    def _put(self, text: str) -> None:
        while len(self.lines) <= self.row:
            self.lines.append("")
        line = self.lines[self.row]
        if len(line) < self.col:
            line += " " * (self.col - len(line))
        self.lines[self.row] = (line[: self.col] + text + line[self.col + len(text) :])[: self.width]
        self.col = min(self.width - 1, self.col + len(text))

    def _newline(self) -> None:
        self.row += 1
        self.col = 0
        while len(self.lines) <= self.row:
            self.lines.append("")

    def _escape(self, params: str, final: str) -> None:
        numbers = [int(part) if part.isdigit() else 0 for part in params.split(";") if part != ""] or [0]
        count = numbers[0] or 1
        if final == "A":
            self.row = max(0, self.row - count)
        elif final == "B":
            self.row += count
            while len(self.lines) <= self.row:
                self.lines.append("")
        elif final == "C":
            self.col = min(self.width - 1, self.col + count)
        elif final == "D":
            self.col = max(0, self.col - count)
        elif final == "G":
            self.col = max(0, min(self.width - 1, count - 1))
        elif final == "K":
            while len(self.lines) <= self.row:
                self.lines.append("")
            self.lines[self.row] = self.lines[self.row][: self.col]
        elif final == "J":
            while len(self.lines) <= self.row:
                self.lines.append("")
            self.lines[self.row] = self.lines[self.row][: self.col]
            del self.lines[self.row + 1 :]
        # SGR (m), private modes (h/l), DECRQM replies/queries (p) and other
        # finals carry no layout state this model needs.


class TuiPty:
    """One ``python -m voidcode`` TUI in a real POSIX pseudo-terminal."""

    def __init__(
        self,
        workspace: Path,
        state: Path,
        *,
        width: int = 80,
        height: int = 24,
        session_id: str | None = None,
    ) -> None:
        master, slave = os.openpty()
        self.master = master
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        env = dict(os.environ)
        env.pop("NO_COLOR", None)
        env.pop("COLORTERM", None)
        env.update({"TERM": "ansi", "COLORFGBG": "15;0", "PYTHONPATH": str(_ROOT / "src")})
        args = [sys.executable, "-m", "voidcode", "--workspace", str(workspace), "--state-dir", str(state)]
        if session_id:
            args.extend(["--session-id", session_id])
        try:
            self.process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True, cwd=workspace)
        finally:
            os.close(slave)
        self.output = bytearray()
        self.screen = AnsiScreen(width, height)
        self._resize_mark = 0

    # -- I/O ---------------------------------------------------------------

    def _pump(self, timeout: float = 0.05) -> bool:
        readable, _, _ = select.select([self.master], [], [], timeout)
        if not readable:
            return self.process.poll() is None
        try:
            chunk = os.read(self.master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            return False
        if not chunk:
            return False
        self.output.extend(chunk)
        self.screen.feed(chunk)
        return True

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    # -- waiting -----------------------------------------------------------

    def until(self, marker: str, *, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.screen.visible():
                return
            if not self._pump():
                break
        raise AssertionError(f"terminal never displayed {marker!r}; exit={self.process.poll()}, tail={self.screen.visible()[-3000:]!r}")

    def until_screen(self, predicate: Callable[[AnsiScreen], bool], what: str, *, timeout: float = 30.0) -> None:
        """Pump until ``predicate`` sees the current screen state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.screen):
                return
            if not self._pump():
                break
        raise AssertionError(f"terminal never showed {what}; exit={self.process.poll()}, tail={self.screen.visible()[-3000:]!r}")

    def until_gone(self, marker: str, *, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump()
            if marker not in self.screen.visible():
                return
        raise AssertionError(f"terminal still displayed {marker!r}; exit={self.process.poll()}, tail={self.screen.visible()[-3000:]!r}")

    def settle(self, seconds: float = 0.6) -> str:
        """Pump for a fixed window and return the visible screen."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._pump()
        return self.screen.visible()

    def await_repaint(self, *, timeout: float = 6.0, quiet: float = 0.25) -> str:
        """Wait for at least one frame after ``resize`` and for output to go quiet."""
        deadline = time.monotonic() + timeout
        last_change: float | None = None
        while time.monotonic() < deadline:
            before = len(self.output)
            self._pump()
            now = time.monotonic()
            if len(self.output) > before:
                last_change = now
            if last_change is not None and len(self.output) > self._resize_mark and now - last_change >= quiet:
                return self.screen.visible()
        raise AssertionError(f"no repaint after resize; exit={self.process.poll()}, tail={self.screen.visible()[-3000:]!r}")

    # -- lifecycle ---------------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        """Change the PTY window size and deliver SIGWINCH to the TUI process."""
        self._resize_mark = len(self.output)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        self.screen.resize(width, height)
        os.kill(self.process.pid, signal.SIGWINCH)

    def quit(self) -> None:
        self.send(b"\x03\x03")
        assert self.process.wait(timeout=10) == 0

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        os.close(self.master)

    def __enter__(self) -> TuiPty:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
