"""Real PTY acceptance for detail actions, prepared forms and passive replay."""

from __future__ import annotations

import errno
import json
import os
import re
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from voidcode.bootstrap import bundled_sources
from voidcode.core.storage.store import SqliteStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX PTY support")


class _TerminalProcess:
    def __init__(self, workspace: Path, state: Path, *, width: int, height: int, palette: str, session_id: str | None = None):
        import fcntl
        import termios

        master, slave = os.openpty()
        self.master = master
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        env = dict(os.environ)
        env.pop("NO_COLOR", None)
        env.pop("COLORTERM", None)
        env.update({"TERM": "ansi", "COLORFGBG": palette, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
        args = [sys.executable, "-m", "voidcode", "--workspace", str(workspace), "--state-dir", str(state)]
        if session_id:
            args.extend(["--session-id", session_id])
        try:
            self.process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        finally:
            os.close(slave)
        self.output = bytearray()

    @property
    def text(self) -> str:
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", self.output.decode("utf-8", errors="replace"))

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def until(self, marker: str, *, timeout: float = 12) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.text:
                return
            readable, _, _ = select.select([self.master], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                self.output.extend(chunk)
            elif self.process.poll() is not None:
                break
        raise AssertionError(f"Terminal never displayed {marker!r}; exit={self.process.poll()}, tail={self.text[-3000:]!r}")

    def quit(self) -> None:
        self.send(b"\x03\x03")
        assert self.process.wait(timeout=8) == 0

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        os.close(self.master)


@pytest.mark.parametrize("width,height,palette", [(80, 24, "15;0"), (40, 18, "0;15")])
def test_todo_add_actions_and_restart_replay_in_real_terminal(tmp_path: Path, width: int, height: int, palette: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins["todo"] = {"enabled": True}
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": plugins}))
    terminal = _TerminalProcess(workspace, state, width=width, height=height, palette=palette)
    try:
        terminal.until("no model")
        footer_spans = re.findall(r"\x1b\[([0-9;]+)m([^\x1b]*no model[^\x1b]*)", terminal.output.decode("utf-8", errors="replace"))
        assert any("39" in codes.split(";") for codes, _text in footer_spans), "Footer did not use terminal default foreground"
        terminal.send(b"/todo\r")
        terminal.until("Add tasks")
        terminal.send(b"\r")
        terminal.until("Tasks (one per line)")
        terminal.send(b"\t\x1b[200~PTY first\nPTY second\x1b[201~\x13")
        terminal.until("Edit: PTY second")
        terminal.quit()
    finally:
        terminal.close()
    store = SqliteStore(state / "voidcode.db")
    try:
        session_id = store.sessions(workspace=str(workspace))[0].session_id
        data, _version = store.plugin_state("todo", "session", session_id)
        assert data["revision"] == 1
        assert [task["content"] for task in data["phases"][0]["tasks"]] == ["PTY first", "PTY second"]
        assert [task["status"] for task in data["phases"][0]["tasks"]] == ["in_progress", "pending"]
    finally:
        store.close()
    reopened = _TerminalProcess(workspace, state, width=width, height=height, palette=palette, session_id=session_id)
    try:
        reopened.until("PTY first")
        reopened.until("no model")
        assert "Tab action" not in reopened.text, "History replay reopened an interactive detail"
        reopened.quit()
    finally:
        reopened.close()
