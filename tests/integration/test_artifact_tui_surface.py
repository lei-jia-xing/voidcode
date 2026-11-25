"""Headless PTY proof of the real CLI's Kitty/state protocol, not physical pixels."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fixtures.png import decode_rgb_png

_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = "artifact-pty-token"
_PROTOCOL = re.compile(rb"\x1b\[\?(\d+)\$p|\x1b_G(.*?)\x1b\\", re.DOTALL)


@pytest.fixture(scope="module")
def artifact_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, Any]]]:
    directory = tmp_path_factory.mktemp("artifact-pty-scene")
    workspace, state = directory / "workspace", directory / "state"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(_ROOT), str(_ROOT / "src")))}
    prepared = subprocess.run(
        [sys.executable, str(_ROOT / "frontend/e2e/artifact_scene.py"), str(workspace), str(state)],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    scene = json.loads(prepared.stdout.strip().splitlines()[-1])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryFile(mode="w+") as logs:
        server = subprocess.Popen(
            [
                str(_ROOT / ".venv/bin/voidcode"),
                "--workspace",
                str(workspace),
                "--state-dir",
                str(state),
                "serve",
                "--port",
                str(port),
                "--bearer-token",
                _TOKEN,
            ],
            cwd=_ROOT,
            env=env,
            stdout=logs,
            stderr=logs,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    logs.seek(0)
                    raise AssertionError(logs.read())
                try:
                    if httpx.get(base + "/api/health", headers={"Authorization": f"Bearer {_TOKEN}"}, timeout=0.2).is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.02)
            else:
                raise AssertionError("Core server did not become ready")
            yield base, scene
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


class KittyPty:
    """A validating protocol peer; real PNG CRC/scanlines determine upload replies."""

    def __init__(self, base: str, session_id: str, *, supported: bool, browser: str | None = None) -> None:
        self.supported = supported
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 1200, 640))
        self.output = bytearray()
        self.pending = bytearray()
        self.commands: list[dict[bytes, bytes]] = []
        self.uploads: list[tuple[int, bytes, str | None]] = []
        self.images: dict[int, bytes] = {}
        self.current_upload: tuple[int, list[bytes]] | None = None
        self.probes = 0
        env = {**os.environ, "TERM": "xterm-kitty", "NO_COLOR": "1", "PYTHONPATH": os.pathsep.join((str(_ROOT), str(_ROOT / "src")))}
        if browser is not None:
            env["BROWSER"] = browser
        self.process = subprocess.Popen(
            [str(_ROOT / ".venv/bin/voidcode"), "--server", base, "--token", _TOKEN, "--session-id", session_id, "--footer", "off"],
            cwd=_ROOT,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def consume(self, chunk: bytes) -> None:
        self.output.extend(chunk)
        self.pending.extend(chunk)
        while match := _PROTOCOL.search(self.pending):
            if match.group(1):
                mode = int(match.group(1))
                state = 1 if mode in (7, 25) else 2
                self.write(f"\x1b[?{mode};{state}$y".encode())
            else:
                header, _, payload = match.group(2).partition(b";")
                fields = dict(item.split(b"=", 1) for item in header.split(b",") if b"=" in item)
                self.commands.append(fields)
                action = fields.get(b"a")
                if action == b"q":
                    self.probes += 1
                    if self.supported:
                        # Unrelated ordinary input beside the correlated response must survive.
                        self.write(b"kept" + b"\x1b_Gi=" + fields[b"i"] + b";OK\x1b\\")
                elif action == b"t" or (action is None and b"m" in fields and self.current_upload is not None):
                    if action == b"t":
                        assert fields[b"f"] == b"100" and fields[b"q"] == b"0"
                        self.current_upload = (int(fields[b"i"]), [])
                    assert len(payload) <= 4096 and self.current_upload is not None
                    image_id, parts = self.current_upload
                    parts.append(base64.b64decode(payload, validate=True))
                    if fields.get(b"m", b"0") == b"0":
                        data = b"".join(parts)
                        error = None
                        try:
                            decode_rgb_png(data)
                        except (ValueError, struct.error) as failure:
                            error = f"EINVAL:{failure}"
                        self.uploads.append((image_id, data, error))
                        if error is None:
                            self.images[image_id] = data
                        self.write(f"\x1b_Gi={image_id};{error or 'OK'}\x1b\\".encode())
                        self.current_upload = None
                elif action == b"p":
                    assert fields[b"C"] == b"1" and fields[b"q"] == b"1" and b"r" not in fields
                    if int(fields[b"i"]) not in self.images:
                        self.write(b"\x1b_Gi=" + fields[b"i"] + b",p=" + fields[b"p"] + b";ENOENT:missing image\x1b\\")
                elif action == b"d" and fields.get(b"d") == b"I":
                    self.images.pop(int(fields[b"i"]), None)
            del self.pending[: match.end()]
        last_escape = self.pending.rfind(b"\x1b")
        if last_escape >= 0:
            del self.pending[:last_escape]
        else:
            self.pending.clear()

    def wait_for(self, predicate: Callable[[], bool], *, seconds: float = 10) -> None:
        deadline = time.monotonic() + seconds
        eof = False
        while not predicate() and time.monotonic() < deadline:
            if not eof:
                ready, _, _ = select.select([self.master], [], [], min(0.05, max(0.0, deadline - time.monotonic())))
                if ready:
                    try:
                        data = os.read(self.master, 65536)
                    except OSError:
                        eof = True
                        data = b""
                    if not data:
                        eof = True
                    else:
                        self.consume(data)
            else:
                time.sleep(0.02)
            if predicate():
                break
            if self.process.poll() is not None and eof:
                break
        if not predicate():
            visible = re.sub(rb"\x1b_G.*?\x1b\\", b"[graphics]", bytes(self.output), flags=re.DOTALL)
            raise AssertionError(f"PTY condition not observed; exit={self.process.poll()}; tail={visible[-5000:]!r}")

    def pump(self, seconds: float = 0.2) -> None:
        deadline = time.monotonic() + seconds
        self.wait_for(lambda: time.monotonic() >= deadline, seconds=seconds + 0.05)

    def placements(self) -> list[dict[bytes, bytes]]:
        return [command for command in self.commands if command.get(b"a") == b"p"]

    def close(self) -> None:
        if self.process.poll() is None:
            self.write(b"\x03\x03")
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        while select.select([self.master], [], [], 0)[0]:
            try:
                remaining = os.read(self.master, 65536)
            except OSError:
                break
            if not remaining:
                break
            self.consume(remaining)
        os.close(self.master)
        assert self.process.returncode == 0


def test_real_cli_kitty_valid_png_reflow_reopen_and_corrupt_error(artifact_server: tuple[str, dict[str, Any]]) -> None:
    base, scene = artifact_server
    terminal = KittyPty(base, scene["image_session"], supported=True)
    try:
        # The graphics probe fires on first image render, not at startup; reply/input
        # separation is covered by tests/unit/tui/test_kitty_replies.py. probes == 1
        # below now also proves the probe runs exactly once.
        terminal.wait_for(lambda: b"PNG_SCENE_REQUEST" in terminal.output)
        terminal.write(b"\x0f")
        terminal.wait_for(lambda: len(terminal.placements()) >= 1)
        image_id, data, error = terminal.uploads[0]
        assert error is None and len(data) == scene["png_bytes"] > 256 * 1024
        assert hashlib.sha256(data).hexdigest() == scene["png_sha256"]
        assert decode_rgb_png(data)[:2] == (384, 300)
        first = terminal.placements()[-1]
        assert int(first[b"i"]) == image_id
        # The PTY reports actual 10x20 cell pixels. Only c is sent; Kitty preserves aspect.
        assert 0 < int(first[b"c"]) <= 120
        assert 300 * int(first[b"c"]) * 10 / (384 * 20) <= 32
        assert terminal.probes == 1
        uploaded = len(terminal.uploads)
        placed = len(terminal.placements())
        fcntl.ioctl(terminal.master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 100, 1000, 720))
        os.kill(terminal.process.pid, signal.SIGWINCH)
        terminal.wait_for(lambda: len(terminal.placements()) > placed)
        assert len(terminal.uploads) == uploaded and int(terminal.placements()[-1][b"i"]) == image_id
        deleted = len([command for command in terminal.commands if command.get(b"a") == b"d"])
        terminal.write(b"\x1b")
        terminal.wait_for(lambda: len([command for command in terminal.commands if command.get(b"a") == b"d"]) > deleted)
        terminal.pump()
        placed = len(terminal.placements())
        terminal.write(b"\x0f")
        terminal.wait_for(lambda: len(terminal.placements()) > placed)
        assert len(terminal.uploads) == uploaded
        terminal.write(b"\x1b")
        terminal.pump()
        terminal.write(b"\x15/tui session " + scene["text_session"].encode() + b"\r")
        terminal.wait_for(lambda: b"SELECTED_INLINE_SUMMARY" in terminal.output)
        terminal.write(b"\x0f")
        terminal.wait_for(lambda: b"Tool artifacts: artifact_scene" in terminal.output)
        for _ in range(6):
            mark = len(terminal.output)
            terminal.write(b"\t")
            terminal.pump(0.25)
            if b"Result body.corrupt_png" in terminal.output[mark:]:
                break
        terminal.wait_for(lambda: len(terminal.uploads) > uploaded)
        corrupt_id, _, failure = terminal.uploads[-1]
        assert failure is not None and "CRC" in failure
        terminal.wait_for(lambda: b"Kitty upload rejected image" in terminal.output)
        assert not any(int(command[b"i"]) == corrupt_id for command in terminal.placements())
        assert b"\x1b[3J" not in terminal.output
        assert bytes(terminal.output).count(b"PNG_SCENE_REQUEST") == 1
    finally:
        terminal.close()
    assert any(command.get(b"a") == b"d" and command.get(b"d") == b"I" for command in terminal.commands)


def test_real_cli_unconfirmed_kitty_uses_explicit_safe_external_copy(artifact_server: tuple[str, dict[str, Any]], tmp_path: Path) -> None:
    base, scene = artifact_server
    opened = tmp_path / "opened.json"
    opener = tmp_path / "open_copy.py"
    opener.write_text(
        "import hashlib,json,os,sys\nfrom pathlib import Path\nfrom urllib.parse import unquote,urlparse\n"
        "path=Path(unquote(urlparse(sys.argv[-1]).path))\n"
        + (
            f"Path({str(opened)!r}).write_text(json.dumps("
            "{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),"
            "'mode':path.stat().st_mode & 0o777}))\n"
        ),
        encoding="utf-8",
    )
    terminal = KittyPty(base, scene["image_session"], supported=False, browser=f"{sys.executable} {opener} %s")
    copied: Path | None = None
    try:
        terminal.wait_for(lambda: b"PNG_SCENE_REQUEST" in terminal.output)
        terminal.write(b"\x0f")
        terminal.wait_for(lambda: b"probe not confirmed" in terminal.output)
        assert not terminal.uploads and terminal.probes == 1 and not opened.exists()
        assert b"image/png" in terminal.output and "384×300".encode() in terminal.output
        assert ("artifact://" + scene["png_sha256"]).encode() in terminal.output
        terminal.write(b"o")
        terminal.wait_for(opened.exists)
        exported = json.loads(opened.read_text())
        copied = Path(exported["path"])
        assert copied.exists() and exported["sha256"] == scene["png_sha256"] and exported["mode"] == 0o600
        assert copied != Path(scene["workspace"]) / "screen"
        assert decode_rgb_png(copied.read_bytes())[:2] == (384, 300)
    finally:
        terminal.close()
    assert copied is not None and not copied.exists()
