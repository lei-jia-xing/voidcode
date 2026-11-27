"""Real PTY acceptance for the generic shortcut + live command-input monitor flow."""

from __future__ import annotations

import errno
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.fixtures.polling import wait_for
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import bundled_sources, open_core
from voidcode.config import dumps_toml
from voidcode.tui.client import CoreRpcClient

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX PTY support")

_CHILD_LABEL = "pty-alpha"
_GRANDCHILD_LABEL = "pty-beta"


class _Screen:
    """Minimal ANSI screen model: tracks what is visibly on screen right now.

    Only the sequences the TUI writer emits are interpreted (cursor moves,
    erase-in-line/display, carriage return). Anything else is visible text.
    """

    _TOKEN = re.compile(
        r"(?P<esc>\x1b\[(?P<params>[0-9;?]*)(?P<final>[@-~]))"
        r"|(?P<cr>\r)"
        r"|(?P<lf>\n)"
        r"|(?P<text>[^\x1b\r\n]+)"
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

    def visible(self) -> str:
        return "\n".join(self.lines[-self.height :])


class _TerminalProcess:
    def __init__(
        self,
        workspace: Path,
        state: Path,
        *,
        width: int,
        height: int,
        palette: str,
        session_id: str | None = None,
        server: str | None = None,
        token: str | None = None,
    ):
        import fcntl
        import termios

        master, slave = os.openpty()
        self.master = master
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        env = dict(os.environ)
        env.pop("NO_COLOR", None)
        env.pop("COLORTERM", None)
        env.update({"TERM": "ansi", "COLORFGBG": palette, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
        if server is not None:
            args = [sys.executable, "-m", "voidcode", "--server", server]
            if token is not None:
                args.extend(["--token", token])
        else:
            args = [sys.executable, "-m", "voidcode", "--workspace", str(workspace), "--state-dir", str(state)]
        if session_id:
            args.extend(["--session-id", session_id])
        try:
            self.process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True, cwd=workspace)
        finally:
            os.close(slave)
        self.output = bytearray()
        self.screen = _Screen(width, height)

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

    def until(self, marker: str, *, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.screen.visible():
                return
            if not self._pump():
                break
        raise AssertionError(f"Terminal never displayed {marker!r}; exit={self.process.poll()}, tail={self.screen.visible()[-3000:]!r}")

    def until_gone(self, marker: str, *, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump(timeout=0.05)
            if marker not in self.screen.visible():
                return
        raise AssertionError(f"Terminal still displayed {marker!r}; exit={self.process.poll()}, tail={self.screen.visible()[-3000:]!r}")

    def settle(self, seconds: float = 0.6) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._pump(timeout=0.05)
        return self.screen.visible()

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


def _write_config(workspace: Path, server: OpenAILoopbackServer, *, subagents: bool = True) -> None:
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins["subagents"] = {"enabled": subagents}
    plugins["providers"] = {"enabled": True}
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": plugins,
            }
        )
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


_LIVEMON_FIXTURE = """
from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolEffect

_SCHEMA = {"type": "object", "properties": {"marker": {"type": "string"}}, "additionalProperties": False}


class SpawnOne:
    def __init__(self, command_id: str) -> None:
        self.definition = CommandDefinition(
            name=command_id,
            description="Spawn one child of the invoking session for monitor smoke.",
            argument_schema=_SCHEMA,
            effects=frozenset({ToolEffect.SPAWN}),
            busy_gate=False,
        )

    def invoke(self, invocation, services):
        marker = str(invocation.arguments.get("marker", "LIVE"))
        child = services.spawn_child(f"smoke work {marker}", label=f"{marker}-label")
        return CommandResult(status="ok", output=child, data={"child": child})


def create_plugin(config):
    return Plugin(commands=(SpawnOne(str(config.get("command_id", "livemon.spawn_one"))),))
"""


def _write_live_config(workspace: Path, server: OpenAILoopbackServer, plugin_dir: Path) -> None:
    """Config with two fixture owners sharing one factory: cross-plugin evidence."""
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins["subagents"] = {"enabled": True}
    plugins["providers"] = {"enabled": True}
    plugins["livemon"] = {"path": str(plugin_dir), "enabled": True, "config": {"command_id": "livemon.spawn_one"}}
    plugins["livemon2"] = {"path": str(plugin_dir), "enabled": True, "config": {"command_id": "livemon2.spawn_one"}}
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": plugins,
            }
        )
    )


@pytest.mark.parametrize("width,height,palette", [(80, 24, "15;0"), (40, 18, "0;15")])
def test_monitor_shortcut_opens_restored_roster_and_restores_draft(tmp_path: Path, width: int, height: int, palette: str) -> None:
    """Alt+A shows the restored persisted roster with both nested labels; close restores draft/caret by edit."""
    replies = [
        openai_response(message={"role": "assistant", "content": "child answer", "tool_calls": []}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "grandchild answer", "tool_calls": []}, finish="stop"),
    ]
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state = tmp_path / "state"
        _write_config(workspace, server)
        with open_core(workspace, state_dir=state) as core:
            root = core.create_session("pty-root").session_id
            child = core.spawn_child(root, "pty child work", label=_CHILD_LABEL, notify_on_completion=False)
            wait_for(lambda: core.session(child).status == "completed", what="child completion")
            grandchild = core.spawn_child(child, "pty grandchild work", label=_GRANDCHILD_LABEL, notify_on_completion=False)
            wait_for(lambda: core.session(grandchild).status == "completed", what="grandchild completion")
        terminal = _TerminalProcess(workspace, state, width=width, height=height, palette=palette, session_id=root)
        try:
            terminal.until("exact-mode")
            # Draft abcd with the caret at offset 2: close must restore it, so Z
            # typed afterwards lands in the middle.
            terminal.send(b"abcd\x1b[D\x1b[D")
            terminal.until("abcd")
            time.sleep(0.05)
            terminal.send(b"\x1ba")  # Alt+A: legacy ESC prefix chord
            terminal.until("Running subagents")
            terminal.until(_CHILD_LABEL)
            terminal.until(_GRANDCHILD_LABEL)
            visible = terminal.settle(0.05)
            assert "Running subagents" in visible, f"roster not on screen:\n{visible}"
            assert _CHILD_LABEL in visible, f"child label not on screen:\n{visible}"
            assert _GRANDCHILD_LABEL in visible, f"grandchild label not on screen:\n{visible}"
            terminal.send(b"\x1b")  # lone Esc closes the roster
            terminal.until_gone("Running subagents")
            terminal.send(b"Z")
            terminal.until("abZcd")
            visible = terminal.settle(0.05)
            assert "abZcd" in visible, f"caret not restored at offset 2:\n{visible}"
            assert "/subagents monitor" not in visible, f"hidden action leaked a slash path:\n{visible}"
            terminal.quit()
        finally:
            terminal.close()
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_monitor_empty_roster_stays_open_and_refreshable(tmp_path: Path) -> None:
    """No children: the roster opens on an explicit empty state and stays mounted."""
    server = OpenAILoopbackServer([])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state = tmp_path / "state"
        _write_config(workspace, server)
        with open_core(workspace, state_dir=state) as core:
            root = core.create_session("pty-empty").session_id
        terminal = _TerminalProcess(workspace, state, width=80, height=24, palette="15;0", session_id=root)
        try:
            terminal.until("exact-mode")
            terminal.send(b"\x1ba")
            terminal.until("Running subagents")
            visible = terminal.settle()
            assert "Running subagents" in visible, f"empty roster not mounted:\n{visible}"
            assert "No options" in visible, f"empty state missing:\n{visible}"
            terminal.send(b"\r")  # Enter on zero options must not dispatch/cancel
            visible = terminal.settle(0.05)
            assert "Running subagents" in visible, f"empty roster closed on Enter:\n{visible}"
            terminal.send(b"\x1b")
            terminal.until_gone("Running subagents")
            terminal.quit()
        finally:
            terminal.close()
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_monitor_open_view_invalidates_on_remote_plugin_disable(tmp_path: Path) -> None:
    """While the roster is open, a remote reload disabling delegation visibly drops it and the chord."""
    server = OpenAILoopbackServer([])
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    proc: subprocess.Popen[bytes] | None = None
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        state = tmp_path / "state"
        _write_config(workspace, server)
        port = _free_port()
        token = "pty-disable-token"
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"), "PYTHONUNBUFFERED": "1"})
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "voidcode",
                "--workspace",
                str(workspace),
                "--state-dir",
                str(state),
                "serve",
                "--port",
                str(port),
                "--bearer-token",
                token,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        base = f"http://127.0.0.1:{port}"

        def _healthy() -> bool:
            try:
                with CoreRpcClient(base, token=token) as probe:
                    return probe.health().status == "ok"
            except Exception as exc:
                if proc.poll() is not None:
                    output = (proc.stdout.read() if proc.stdout else b"").decode("utf-8", errors="replace")
                    raise AssertionError(f"served core exited early: {output[-2000:]}") from exc
                return False

        wait_for(_healthy, what="served core health")
        with CoreRpcClient(base, token=token) as client:
            root = client.create_session("pty-remote-disable").session_id
            terminal = _TerminalProcess(workspace, state, width=80, height=24, palette="15;0", session_id=root, server=base, token=token)
            try:
                terminal.until("exact-mode")
                terminal.send(b"\x1ba")
                terminal.until("Running subagents")
                # Disable delegation and reload through RPC while the roster is
                # open: the activation must visibly drop the live view.
                _write_config(workspace, server, subagents=False)
                revision = client.command_catalog().revision_id
                result = client.invoke_command(root, "core.reload", {}, expected_revision=revision)
                assert result.status == "ok", f"core.reload failed: {result.error}"
                terminal.until("pending action discarded after configuration changed")
                terminal.until_gone("Running subagents")
                visible = terminal.settle()
                assert "Running subagents" not in visible, f"stale roster still on screen:\n{visible}"
                # The chord is dead after disable: Alt+A opens nothing new.
                terminal.send(b"\x1ba")
                visible = terminal.settle(0.1)
                assert "Running subagents" not in visible, f"disabled binding reopened the roster:\n{visible}"
                terminal.quit()
            finally:
                terminal.close()
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _turn(text: str) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": text, "tool_calls": []}, finish="stop")


class _LiveScene:
    """Gated live scene on a served core: child+grandchild(cross-plugin)+sibling+running root."""

    def __init__(self, tmp_path: Path, *, token: str, root_name: str) -> None:
        self.tmp_path = tmp_path
        self.token = token
        self.root_name = root_name
        self.server = OpenAILoopbackServer([_turn("done")] * 30)
        self.gate = threading.Event()
        self.entered: dict[str, threading.Event] = {}
        self.proc: subprocess.Popen[bytes] | None = None
        self.serve_log: list[bytes] = []
        self.base = ""
        self.root = ""
        self.child_id = ""
        self.grandchild_id = ""
        self.sibling_id = ""
        self.run_outcome: dict[str, object] = {}
        self.run_thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._client: CoreRpcClient | None = None

    def __enter__(self) -> _LiveScene:
        from voidcode.sdk.client import RunSpec

        def _hold(payload: dict[str, object], index: int):  # type: ignore[no-untyped-def]
            text = json.dumps(payload.get("messages"), ensure_ascii=False)
            for marker in ("LIVE-CHILD", "LIVE-GRAND", "LIVE-SIB", "LIVE-ROOT"):
                if marker in text:
                    self.entered.setdefault(marker, threading.Event()).set()
                    break
            assert self.gate.wait(timeout=90), "live scene never released the provider gate"
            return None

        self.server.before_reply = _hold
        self._worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._worker.start()
        workspace = self.tmp_path / "workspace"
        workspace.mkdir()
        state = self.tmp_path / "state"
        plugin_dir = self.tmp_path / "livemon_plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "__init__.py").write_text(_LIVEMON_FIXTURE, encoding="utf-8")
        _write_live_config(workspace, self.server, plugin_dir)
        port = _free_port()
        env = dict(os.environ)
        env.update({"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"), "PYTHONUNBUFFERED": "1"})
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "voidcode",
                "--workspace",
                str(workspace),
                "--state-dir",
                str(state),
                "serve",
                "--port",
                str(port),
                "--bearer-token",
                self.token,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        self.base = f"http://127.0.0.1:{port}"

        def _drain() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            while True:
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    return
                self.serve_log.append(chunk)

        threading.Thread(target=_drain, daemon=True).start()

        def _healthy() -> bool:
            try:
                with CoreRpcClient(self.base, token=self.token) as probe:
                    return probe.health().status == "ok"
            except Exception as exc:
                assert self.proc is not None
                if self.proc.poll() is not None:
                    output = b"".join(self.serve_log).decode("utf-8", errors="replace")
                    raise AssertionError(f"served core exited early: {output[-2000:]}") from exc
                return False

        wait_for(_healthy, what="served core health")
        self._client = CoreRpcClient(self.base, token=self.token)
        client = self._client
        self.root = client.create_session(self.root_name).session_id
        revision = client.command_catalog().revision_id
        child = client.invoke_command(self.root, "livemon.spawn_one", {"marker": "LIVE-CHILD"}, expected_revision=revision)
        assert child.status == "ok", child.error
        self.child_id = str(child.data["child"])
        assert self.entered.setdefault("LIVE-CHILD", threading.Event()).wait(timeout=30), "child never hit provider"
        grandchild = client.invoke_command(self.child_id, "livemon2.spawn_one", {"marker": "LIVE-GRAND"}, expected_revision=revision)
        assert grandchild.status == "ok", grandchild.error
        self.grandchild_id = str(grandchild.data["child"])
        assert self.entered.setdefault("LIVE-GRAND", threading.Event()).wait(timeout=30), "grandchild never hit provider"
        sibling = client.invoke_command(self.root, "livemon.spawn_one", {"marker": "LIVE-SIB"}, expected_revision=revision)
        assert sibling.status == "ok", sibling.error
        self.sibling_id = str(sibling.data["child"])
        assert self.entered.setdefault("LIVE-SIB", threading.Event()).wait(timeout=30), "sibling never hit provider"
        assert client.get_session(self.child_id).status == "running"
        assert client.get_session(self.grandchild_id).status == "running"
        assert client.get_session(self.sibling_id).status == "running"

        def _run_root() -> None:
            try:
                with CoreRpcClient(self.base, token=self.token) as runner:
                    self.run_outcome["result"] = runner.run(self.root, RunSpec("root work LIVE-ROOT"))
            except Exception as exc:  # noqa: BLE001 - asserted on release
                self.run_outcome["error"] = exc

        self.run_thread = threading.Thread(target=_run_root, daemon=True)
        self.run_thread.start()
        assert self.entered.setdefault("LIVE-ROOT", threading.Event()).wait(timeout=30), "root run never hit provider"
        assert client.get_session(self.root).status == "running"
        return self

    @property
    def client(self) -> CoreRpcClient:
        assert self._client is not None
        return self._client

    @property
    def workspace(self) -> Path:
        return self.tmp_path / "workspace"

    @property
    def state(self) -> Path:
        return self.tmp_path / "state"

    def release_and_join_root(self) -> None:
        self.gate.set()
        assert self.run_thread is not None
        self.run_thread.join(timeout=90)
        assert self.run_thread.is_alive() is False, "root run thread never returned"
        assert "error" not in self.run_outcome, self.run_outcome["error"]
        assert self.run_outcome["result"].status == "completed", self.run_outcome["result"]

    def __exit__(self, *args: object) -> None:
        self.gate.set()
        if self._client is not None:
            self._client.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=8)
        self.server.shutdown()
        if self._worker is not None:
            self._worker.join(timeout=5)
        self.server.server_close()


def test_live_remote_tui_browses_gated_roster_and_detail(tmp_path: Path) -> None:
    """Remote TUI browses a gated live scene: roster labels, detail, draft restore, no cancel yet."""
    with _LiveScene(tmp_path, token="pty-live-token", root_name="pty-live-browse") as scene:
        terminal = _TerminalProcess(
            scene.workspace, scene.state, width=80, height=24, palette="15;0", session_id=scene.root, server=scene.base, token=scene.token
        )
        try:
            terminal.until("exact-mode")
            terminal.send(b"draft-live\x1b[D\x1b[D")
            terminal.until("draft-live")
            time.sleep(0.05)
            terminal.send(b"\x1ba")
            terminal.until("Running subagents")
            terminal.until("LIVE-CHILD-label")
            terminal.until("LIVE-GRAND-label")
            terminal.until("LIVE-SIB-label")
            visible = terminal.settle(0.05)
            assert "LIVE-CHILD-label" in visible, f"child missing:\n{visible}"
            assert "LIVE-GRAND-label" in visible, f"grandchild missing:\n{visible}"
            assert "LIVE-SIB-label" in visible, f"sibling missing:\n{visible}"
            # Enter the selected detail, then return via its Back action
            # (Esc closes the whole monitor by design).
            terminal.send(b"\r")
            terminal.until("Subagent")
            visible = terminal.settle(0.05)
            assert "Subagent" in visible, f"detail not on screen:\n{visible}"
            assert "Back to roster" in visible, f"back action missing:\n{visible}"
            terminal.send(b"\r")
            terminal.until("Running subagents")
            visible = terminal.settle(0.05)
            assert "LIVE-CHILD-label" in visible, f"roster not restored:\n{visible}"
            terminal.send(b"\x1b")
            terminal.until_gone("Running subagents")
            terminal.send(b"Z")
            terminal.until("draft-liZve")
            visible = terminal.settle(0.05)
            assert "draft-liZve" in visible, f"draft/caret not restored:\n{visible}"
            terminal.quit()
        finally:
            terminal.close()
        scene.release_and_join_root()


def test_live_remote_tui_cancel_deny_preserves_and_allow_settles(tmp_path: Path) -> None:
    """Remote TUI cancels the child subtree: deny leaves work running, allow settles with requested wording."""
    with _LiveScene(tmp_path, token="pty-live-cancel", root_name="pty-live-cancel") as scene:
        client = scene.client
        terminal = _TerminalProcess(
            scene.workspace, scene.state, width=80, height=24, palette="15;0", session_id=scene.root, server=scene.base, token=scene.token
        )
        try:
            terminal.until("exact-mode")
            terminal.send(b"keep-draft\x1b[D\x1b[D")
            terminal.until("keep-draft")
            time.sleep(0.05)
            terminal.send(b"\x1ba")
            terminal.until("Running subagents")
            terminal.until("LIVE-CHILD-label")
            terminal.send(b"\r")
            terminal.until("Back to roster")
            visible = terminal.settle(0.05)
            assert scene.child_id in visible, f"child detail not on screen:\n{visible}"
            assert "Cancel subtree" in visible, f"cancel action missing:\n{visible}"
            # Move to Cancel subtree (second action) and invoke: deny first.
            terminal.send(b"\t")
            terminal.until("❯ Cancel subtree")
            visible = terminal.settle(0.05)
            assert "❯ Cancel subtree" in visible, f"cancel not selected:\n{visible}"
            terminal.send(b"\r")
            terminal.until("Action Confirmation")
            visible = terminal.settle(0.05)
            assert "1 descendant(s)" in visible, f"scope count missing:\n{visible}"
            assert "siblings are unaffected" in visible, f"scope text missing:\n{visible}"
            assert scene.child_id[:8] in visible, f"cancel scope missing:\n{visible}"
            # Deny is the second decision: denied work keeps running. Sync on
            # the confirmation leaving the visible screen.
            terminal.send(b"\t")
            time.sleep(0.05)
            terminal.send(b"\r")
            terminal.until_gone("Action Confirmation")
            visible = terminal.settle(0.05)
            assert "❯ Cancel subtree" in visible, f"cancel selection not retained:\n{visible}"
            assert scene.child_id in visible, f"child detail not retained:\n{visible}"
            assert client.get_session(scene.child_id).status == "running"
            assert client.get_session(scene.grandchild_id).status == "running"
            assert client.get_session(scene.sibling_id).status == "running"
            # One Enter reopens a fresh confirmation for the same target; the
            # default decision is allow.
            terminal.send(b"\r")
            terminal.until("Action Confirmation")
            terminal.send(b"\r")
            terminal.until("Cancellation requested")
            visible = terminal.settle(0.05)
            assert scene.child_id[:8] in visible, f"receipt missing target:\n{visible}"
            scene.gate.set()
            wait_for(lambda: client.get_session(scene.child_id).status == "interrupted", what="child owner-cancel settle")
            wait_for(lambda: client.get_session(scene.grandchild_id).status == "interrupted", what="grandchild owner-cancel settle")
            terminal.until("interrupted")
            visible = terminal.settle(0.05)
            assert "owner_cancel" in visible, f"settled stop cause missing:\n{visible}"
            # One Esc from the confirmed-open detail returns to the composer;
            # typing then proves the retained draft/caret.
            terminal.send(b"\x1b")
            terminal.until_gone("Back to roster")
            terminal.send(b"Z")
            terminal.until("keep-draZft")
            visible = terminal.settle(0.05)
            assert "keep-draZft" in visible, f"draft/caret not restored:\n{visible}"
            terminal.quit()
        finally:
            terminal.close()
        scene.release_and_join_root()
        assert client.get_session(scene.sibling_id).status == "completed"
        assert client.get_session(scene.root).status == "completed"
