"""End-to-end TUI test over a real pty: the inline contract, on the wire.

The CLI is spawned under an allocated pty with the deterministic execution
engine, driven through one prompt, and the raw byte stream is classified into
*committed* rows (``Terminal.commit_rows``: rows written once into native
scrollback) and live frames (``Terminal.paint_frame``: rows diffed in place).
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest
from rich.color import ColorSystem

from voidcode.tui.theme import resolve_theme

pytestmark = pytest.mark.skipif(os.name != "posix" or sys.platform.startswith(("win", "cygwin")), reason="needs a POSIX pty")

_PROMPT = "read source.txt"
_ANSWER = "Read 1 line(s) from source.txt."
_PROMPT_TIMEOUT = 30.0
_ANSWER_TIMEOUT = 60.0
_EXIT_TIMEOUT = 20.0
_COLUMNS = 100
_ROWS = 30

_ENTER_ALT = "\x1b[?1049h"
_LEAVE_ALT = "\x1b[?1049l"
_SYNC_ON = "\x1b[?2026h"
_SYNC_OFF = "\x1b[?2026l"
#: ``commit_rows`` writes plain rows inside one paint bracket; ``paint_frame``
#: prefixes every changed row with CR + erase-line. That is the only difference.
_PAINT_BEGIN = "\x1b[?25l\x1b[?2026h\x1b[?7l"
_ROW_PREFIX = "\r\x1b[2K"
_PAINT_END = "\x1b[?7h\x1b[?2026l"
_ANSI = re.compile(r"\x1b\[[0-9;?<>]*[a-zA-Z]|\x1b\][^\x07]*\x07")


# ---------------------------------------------------------------------------
# pty harness
# ---------------------------------------------------------------------------


def _persisted_tool_call_id(db_path: Path) -> str:
    """One real tool call id from the runtime's persisted events.

    The transcript never prints ids, and ``/expand`` is documented to take one, so
    the test reads the runtime's own record instead of inventing an id.
    """
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT payload_json FROM session_events").fetchall()
    finally:
        connection.close()
    for (payload,) in rows:
        event = json.loads(payload)
        tool_call_id = event.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            return tool_call_id
    return ""


def _session_status(db_path: Path) -> str:
    """Status of the session the runtime persisted (``interrupted`` after a cancel)."""
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT status FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        connection.close()
    return row[0] if row else ""


def _cli_env(db_path: Path) -> dict[str, str]:
    """Deterministic engine, isolated session DB (never the developer's XDG one)."""
    env = os.environ.copy()
    env["VOIDCODE_EXECUTION_ENGINE"] = "deterministic"
    env["VOIDCODE_DB_PATH"] = str(db_path)
    env.pop("NO_COLOR", None)
    return env


class _PtyTui:
    """One ``voidcode tui`` child on its own pty, plus the raw byte capture."""

    def __init__(self, workspace: Path, db_path: Path) -> None:
        self.master, slave = pty.openpty()
        # A fresh pty reports 0x0; the renderer must be told the real geometry.
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", _ROWS, _COLUMNS, 0, 0))
        env = _cli_env(db_path)
        env["TERM"] = "xterm-256color"
        # Pin the colour depth too: the palette must reach the wire as 24-bit SGR
        # regardless of the developer's own COLORTERM.
        env["COLORTERM"] = "truecolor"
        env["COLUMNS"] = str(_COLUMNS)
        env["LINES"] = str(_ROWS)
        self._raw = bytearray()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "voidcode", "tui", "--workspace", str(workspace)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=workspace,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)

    # -- io ----------------------------------------------------------------

    def _pump(self, deadline: float) -> None:
        timeout = max(0.0, min(0.25, deadline - time.monotonic()))
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return
        try:
            data = os.read(self.master, 65536)
        except OSError:  # EIO: the child side is gone
            return
        self._raw.extend(data)

    def wait_for(self, needle: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.raw:
                return True
            self._pump(deadline)
        return needle in self.raw

    def wait_for_exit(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                while True:  # drain whatever is still buffered
                    ready, _, _ = select.select([self.master], [], [], 0.05)
                    if not ready:
                        break
                    try:
                        data = os.read(self.master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    self._raw.extend(data)
                return code
            self._pump(deadline)
        return None

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    # -- inspection --------------------------------------------------------

    @property
    def raw(self) -> str:
        return self._raw.decode("utf-8", "replace")

    def commit_bodies(self) -> list[str]:
        """Raw bodies of the commit-path writes, escapes intact."""
        bodies: list[str] = []
        text = self.raw
        position = 0
        while True:
            start = text.find(_PAINT_BEGIN, position)
            if start == -1:
                return bodies
            end = text.find(_PAINT_END, start)
            if end == -1:
                return bodies
            body = text[start + len(_PAINT_BEGIN) : end]
            position = end + len(_PAINT_END)
            if _ROW_PREFIX not in body:
                bodies.append(body)

    def committed_rows(self) -> list[str]:
        """Rows the terminal received through the commit path, ANSI stripped."""
        rows: list[str] = []
        text = self.raw
        position = 0
        while True:
            start = text.find(_PAINT_BEGIN, position)
            if start == -1:
                return rows
            end = text.find(_PAINT_END, start)
            if end == -1:
                return rows
            body = text[start + len(_PAINT_BEGIN) : end]
            position = end + len(_PAINT_END)
            if _ROW_PREFIX in body:
                continue  # a live frame, not a commit
            for row in body.split("\r\n"):
                plain = _ANSI.sub("", row).replace("\r", "").strip()
                if plain:
                    rows.append(plain)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait(timeout=10)
        os.close(self.master)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_inline_tui_prompt_response_commit_and_clean_exit(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "source.txt").write_text("hello marker\n", encoding="utf-8")
    tui = _PtyTui(workspace, tmp_path / "sessions.sqlite3")
    try:
        assert tui.wait_for("Ask voidcode", _PROMPT_TIMEOUT), tui.raw[-2000:]
        # The composer is live but nothing is committed yet.
        assert tui.committed_rows() == []

        tui.send(f"{_PROMPT}\r".encode())
        assert tui.wait_for(_ANSWER, _ANSWER_TIMEOUT), tui.raw[-4000:]

        rows = tui.committed_rows()
        raw = tui.raw

        # 1. Zero alternates: a plain prompt->response run never borrows the alt screen.
        assert raw.count(_ENTER_ALT) == 0
        assert raw.count(_LEAVE_ALT) == 0
        # 2. Synchronized-output brackets are balanced.
        assert raw.count(_SYNC_ON) == raw.count(_SYNC_OFF)
        # 3. The typed prompt and the resulting tool/assistant rows are committed,
        #    in raw-stream order, each tape row written once (a settled re-commit
        #    would repeat the whole prefix).
        assert rows.count(_PROMPT) == 1, rows
        assert rows.count("▶ Started tool: read") == 1, rows
        completion = next(row for row in rows if row.startswith("✔") and "source.txt" in row)
        assert _ANSWER in rows, rows
        answer_index = len(rows) - 1 - rows[::-1].index(_ANSWER)
        assert rows.index(_PROMPT) < rows.index("▶ Started tool: read") < rows.index(completion) < answer_index
        # 4. No repaint of a committed row: the live frame holds it once, the
        #    commit writes it once, and ordinary frames never rewrite history.
        assert raw.count(_PROMPT) <= 2, raw.count(_PROMPT)

        # 5. ctrl+o expands every block. Rows of an already-committed block cannot
        #    be rewritten in scrollback, so the settled tape is printed once more
        #    (and only once): the prompt row is handed out a second time.
        tui.send(b"\x0f")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and tui.committed_rows().count(_PROMPT) < 2:
            tui._pump(deadline)
        expanded = tui.committed_rows()
        assert len(expanded) > len(rows), expanded
        assert expanded.count(_PROMPT) == 2, expanded

        # 6. The ported palette reaches the wire: the committed user row carries the
        #    userMessageBg band and the status/tool accents carry `accent`, all as
        #    24-bit SGR. A renderer that stopped applying the theme -- or fell back
        #    to 16-colour/default -- fails here. Expectation is derived from the
        #    theme object, so a palette change moves the guard with it.
        theme = resolve_theme("voidcode-dark", "dark", color_system=ColorSystem.TRUECOLOR)
        band = theme.get_bg_ansi("userMessageBg")
        accent = theme.get_fg_ansi("accent")
        assert band.startswith("\x1b[48;2;") and accent.startswith("\x1b[38;2;"), (band, accent)
        user_body = next(body for body in tui.commit_bodies() if _PROMPT in body)
        assert band in user_body and user_body.index(band) < user_body.index(_PROMPT), user_body[:120]
        assert accent in tui.raw, tui.raw[-500:]  # status state + tool header accent

        # 7. Clean exit: the live region is gone, the protocols are restored, and
        #    the whole run stayed on the normal buffer.
        tui.send(b"\x03\x03")
        assert tui.wait_for_exit(_EXIT_TIMEOUT) == 0, tui.raw[-2000:]
        final = tui.raw
        assert final.count(_ENTER_ALT) == 0
        assert final.count(_LEAVE_ALT) == 0
        assert final.endswith("\x1b[?2004l\x1b[<u\x1b[0m\x1b[?25h"), final[-80:]
    finally:
        tui.close()


def test_inline_tui_configured_keybindings_and_session_picker_alt_screen(tmp_path: Path) -> None:
    """A keymap-bound picker borrows the alt screen, then gives the buffer back."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "source.txt").write_text("hello marker\n", encoding="utf-8")
    (workspace / ".voidcode.json").write_text(
        json.dumps({"tui": {"keymap": {"ctrl+r": "session_resume"}}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "sessions.sqlite3"
    seeded = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "run",
            _PROMPT,
            "--workspace",
            str(workspace),
            "--session-id",
            "seeded-session",
        ],
        capture_output=True,
        text=True,
        timeout=_ANSWER_TIMEOUT,
        env=_cli_env(db_path),
        check=False,
    )
    assert seeded.returncode == 0, seeded.stderr

    tui = _PtyTui(workspace, db_path)
    try:
        assert tui.wait_for("Ask voidcode", _PROMPT_TIMEOUT), tui.raw[-2000:]
        assert tui.raw.count(_ENTER_ALT) == 0
        assert tui.committed_rows() == []

        tui.send(b"\x12")  # the configured session_resume binding
        assert tui.wait_for("Select Session", _PROMPT_TIMEOUT), tui.raw[-3000:]
        assert tui.wait_for(_ENTER_ALT, 5.0), tui.raw[-3000:]
        # While the alternate buffer is borrowed nothing is committed to scrollback.
        assert tui.committed_rows() == []

        tui.send(b"\r")  # select the seeded session
        assert tui.wait_for(_LEAVE_ALT, 10.0), tui.raw[-3000:]
        assert tui.wait_for("Resumed Session", 10.0), tui.raw[-3000:]
        # The resumed session's replay lands in scrollback after the borrow ends.
        assert tui.wait_for(_ANSWER, 20.0), tui.raw[-3000:]

        tui.send(b"\x03\x03")
        assert tui.wait_for_exit(_EXIT_TIMEOUT) == 0, tui.raw[-2000:]

        raw = tui.raw
        assert raw.count(_ENTER_ALT) == 1
        assert raw.count(_LEAVE_ALT) == 1
        rows = tui.committed_rows()
        assert any("Resumed Session" in row for row in rows), rows
        assert _ANSWER in rows
        assert raw.endswith("\x1b[?2004l\x1b[<u\x1b[0m\x1b[?25h"), raw[-80:]
    finally:
        tui.close()


def test_inline_tui_expand_slash_command_reprints_the_fetched_card(tmp_path: Path) -> None:
    """``/expand <id>``: artifact fetch -> expanded card -> settled tape re-printed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "source.txt").write_text("hello marker\n", encoding="utf-8")
    db_path = tmp_path / "sessions.sqlite3"
    tui = _PtyTui(workspace, db_path)
    try:
        assert tui.wait_for("Ask voidcode", _PROMPT_TIMEOUT), tui.raw[-2000:]
        tui.send(f"{_PROMPT}\r".encode())
        assert tui.wait_for(_ANSWER, _ANSWER_TIMEOUT), tui.raw[-3000:]
        before = tui.committed_rows()

        # The transcript does not print tool call ids; read one from the runtime's
        # own persisted events (the id `/expand` is documented to take).
        tool_call_id = _persisted_tool_call_id(db_path)
        assert tool_call_id
        tui.send(f"/expand {tool_call_id}\r".encode())
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not any("╭" in row for row in tui.committed_rows()):
            tui._pump(deadline)
        after = tui.committed_rows()
        assert any("╭" in row and "Read" in row for row in after), after
        assert len(after) > len(before), after

        tui.send(b"\x03\x03")
        assert tui.wait_for_exit(_EXIT_TIMEOUT) == 0, tui.raw[-2000:]
        assert tui.raw.endswith("\x1b[?2004l\x1b[<u\x1b[0m\x1b[?25h"), tui.raw[-80:]
    finally:
        tui.close()


def test_inline_tui_escape_cancels_the_active_turn(tmp_path: Path) -> None:
    """Esc is delivered as ``escape`` and interrupts the run through the runtime."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "source.txt").write_text("hello marker\n", encoding="utf-8")
    db_path = tmp_path / "sessions.sqlite3"
    tui = _PtyTui(workspace, db_path)
    try:
        assert tui.wait_for("Ask voidcode", _PROMPT_TIMEOUT), tui.raw[-2000:]
        # One turn with sixty steps, so the escape lands mid-flight. The composer
        # inserts a newline on shift+enter only (enter submits), hence the kitty
        # sequence instead of "\n".
        prompt = "\x1b[13;2u".join(f"{_PROMPT}" for _ in range(60))
        tui.send(f"{prompt}\r".encode())
        assert tui.wait_for("▶ Started tool: read", _PROMPT_TIMEOUT), tui.raw[-2000:]

        tui.send(b"\x1b")
        assert tui.wait_for("■ Turn cancel requested", _PROMPT_TIMEOUT), tui.raw[-2000:]
        deadline = time.monotonic() + _ANSWER_TIMEOUT
        while time.monotonic() < deadline and _session_status(db_path) != "interrupted":
            tui._pump(deadline)
        assert _session_status(db_path) == "interrupted"

        tui.send(b"\x03\x03")
        assert tui.wait_for_exit(_EXIT_TIMEOUT) == 0, tui.raw[-2000:]
        assert tui.raw.endswith("\x1b[?2004l\x1b[<u\x1b[0m\x1b[?25h"), tui.raw[-80:]
    finally:
        tui.close()


def test_inline_tui_piped_stdout_keeps_no_alternate_screen_and_no_raw_escapes(tmp_path: Path) -> None:
    """Off-tty: the same app degrades to plain lines (no escapes, no alt screen)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "source.txt").write_text("hello marker\n", encoding="utf-8")
    env = _cli_env(tmp_path / "sessions.sqlite3")
    env["TERM"] = "dumb"
    result = subprocess.run(
        [sys.executable, "-m", "voidcode", "tui", "--workspace", str(workspace)],
        input=f"{_PROMPT}\r",
        capture_output=True,
        text=True,
        timeout=_ANSWER_TIMEOUT,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "\x1b" not in result.stderr
    # A non-tty terminal is a log: durable rows only, no status line or composer.
    assert _PROMPT in result.stdout
    assert _ANSWER in result.stdout
    assert "Ask voidcode" not in result.stdout
