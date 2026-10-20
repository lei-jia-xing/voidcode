"""The real supervisor process: cold start, live client surfaces, and process cleanup.

Each case launches the actual ``run_supervisor`` in a subprocess over a real workspace, so what is
asserted is the observable contract a client gets: the exact stdout lines, the API bearer reaching
``/api/health``, the AF_UNIX status frame, control authority staying separate from the API token,
and no orphan child after the supervisor is killed.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from voidcode.kernel.bootstrap import state_directory
from voidcode.kernel.supervisor import control_socket_path

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_READY_TIMEOUT = 120.0

_BOOT = """
import sys
from pathlib import Path
from voidcode.kernel.supervisor import run_supervisor

sys.exit(
    run_supervisor(
        workspace=Path(sys.argv[1]),
        state_dir=Path(sys.argv[2]),
        host="127.0.0.1",
        port=0,
        bearer_token=None,
        trusted_plugins=frozenset(),
        session_id=None,
        open_browser=False,
    )
)
"""


class _Running:
    """One live supervisor subprocess plus the lines it printed before it served."""

    def __init__(self, workspace: Path, state_dir: Path, *, command: list[str] | None = None) -> None:
        (workspace / "voidcode.json").write_text(
            json.dumps({"schema_version": 1, "model": {"model": None}, "policy": {}, "providers": {}}),
            encoding="utf-8",
        )
        argv = command or [sys.executable, "-X", "utf8", "-c", _BOOT, str(workspace), str(state_dir)]
        self.process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.state_dir = state_dir
        self.lines: list[str] = []
        self.endpoint = ""
        self.token = ""
        assert self.process.stdout is not None
        try:
            deadline = time.monotonic() + _READY_TIMEOUT
            while time.monotonic() < deadline:
                line = self.process.stdout.readline()
                if not line:
                    if self.process.poll() is not None:
                        raise AssertionError(f"supervisor exited early: {self.process.stderr.read() if self.process.stderr else ''}")
                    continue
                text = line.strip()
                self.lines.append(text)
                if text.startswith("kernel endpoint: "):
                    self.endpoint = text.partition(": ")[2].strip()
                elif text.startswith("kernel bearer token: "):
                    self.token = text.partition(": ")[2].strip()
                elif text.startswith("kernel ready: "):
                    return
            raise AssertionError(f"supervisor did not become ready: {self.lines}")
        except BaseException:
            # A failed readiness probe must not leave the subprocess (or its pipes) behind.
            self.close()
            raise

    def health(self, *, token: str | None = None) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(f"{self.endpoint}/api/health")
        request.add_header("authorization", f"Bearer {self.token if token is None else token}")
        try:
            with _OPENER.open(request, timeout=10.0) as response:
                return int(response.status), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), json.loads(exc.read() or b"{}")

    def post_control(self, path: str, token: str) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(f"{self.endpoint}{path}", data=b"{}", method="POST")
        request.add_header("authorization", f"Bearer {token}")
        request.add_header("content-type", "application/json")
        try:
            with _OPENER.open(request, timeout=10.0) as response:
                return int(response.status), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return int(exc.code), json.loads(exc.read() or b"{}")

    def status(self) -> dict[str, object]:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(15.0)
        try:
            client.connect(str(control_socket_path(self.state_dir)))
            client.sendall(b'{"op":"status"}\n')
            buffer = b""
            while b"\n" not in buffer:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer += chunk
        finally:
            client.close()
        return json.loads(buffer.split(b"\n", 1)[0])

    def child_pid(self) -> int:
        pid = self.status()["pid"]
        assert isinstance(pid, int)
        return pid

    def close(self, *, sig: int = signal.SIGTERM) -> int:
        if self.process.poll() is None:
            self.process.send_signal(sig)
        try:
            return self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(timeout=15)
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()
            if self.process.stderr is not None:
                self.process.stderr.close()


def _dead(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # A zombie still answers signal 0; reaping is the parent's job, so look at the state too.
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists():
            return True
        fields = stat.read_text(encoding="utf-8").rsplit(")", 1)[-1].split()
        if fields and fields[0] == "Z":
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _coherent_installation() -> None:
    """Require a coherent bundled-plugin set: every real image refuses a drifted tree.

    Admission verifies each manifest against the bytes it pins, so a checkout that is mid-edit
    (a peer changing a bundled plugin without regenerating identities) cannot boot any kernel at
    all. That is correct product behavior, not something these tests may paper over, so they
    declare the precondition and skip instead of reporting a supervisor failure.
    """
    drifted = _drifted_bundled_files()
    if drifted:
        pytest.skip(f"working tree has drifted bundled plugin identities: {', '.join(drifted)}")


def _drifted_bundled_files() -> list[str]:
    import hashlib

    from voidcode.kernel.images import running_package_root

    drifted: list[str] = []
    for manifest_path in sorted((running_package_root() / "plugins").glob("*/voidcode-plugin.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("files", []):
            actual = hashlib.sha256((manifest_path.parent / item["path"]).read_bytes()).hexdigest()
            if actual != item["sha256"]:
                drifted.append(f"{manifest_path.parent.name}/{item['path']}")
    return drifted


@pytest.fixture
def running(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instance = _Running(workspace, tmp_path / "state")
    try:
        yield instance
    finally:
        instance.close()


def test_default_state_dir_startup_and_separate_control_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default launch scheme: no ``--state-dir``, ``$HOME/.local/state`` derivation.

    ``XDG_STATE_HOME`` is unset so ``state_directory`` takes its real default branch, while
    ``HOME`` points inside this test's own pytest tree: the durable state, the image copies, and
    the socket cleanup all stay test-owned, and the old address is still longer than an AF_UNIX
    address may be. Fast's separate PTY smoke covers the actual user HOME surface. Asserted here:
    the default CLI starts, a *separate* CLI process derives the same control address and can
    query it, and shutdown removes the socket.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    default_state = state_directory(workspace)
    socket_path = control_socket_path(default_state)
    assert default_state.is_relative_to(home / ".local" / "state" / "voidcode")
    assert not default_state.exists(), "the test workspace's default state must be fresh"
    assert len(os.fsencode(default_state / "supervisor.sock")) > 104
    assert len(os.fsencode(socket_path)) <= 104

    command = [sys.executable, "-X", "utf8", "-m", "voidcode.kernel", "serve", f"--workspace={workspace}", "--port=0"]
    running: _Running | None = None
    try:
        running = _Running(workspace, default_state, command=command)
        assert socket_path.stat().st_mode & 0o777 == 0o600
        assert running.status()["status"] == "ok"

        client = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "voidcode.kernel",
                "--workspace",
                str(workspace),
                "upgrade",
                "status",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert client.returncode == 0, client.stdout + client.stderr
        answer = json.loads(client.stdout)
        assert answer["status"] == "ok"
        assert answer["pid"] == running.process.pid

        explicit = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "voidcode.kernel",
                "--workspace",
                str(workspace),
                "--state-dir",
                str(default_state),
                "upgrade",
                "status",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert explicit.returncode == 0, explicit.stdout + explicit.stderr
        assert json.loads(explicit.stdout)["status"] == "ok"
    finally:
        if running is not None:
            running.close()
            assert not socket_path.exists()


def test_cold_start_prints_the_client_contract_and_serves_health(running: _Running) -> None:
    assert running.lines[0] == f"kernel endpoint: {running.endpoint}"
    assert running.lines[1] == f"kernel bearer token: {running.token}"
    assert running.lines[2] == f"web client: {running.endpoint}/"
    assert running.lines[3] == f"kernel ready: {running.endpoint} generation=1"
    assert len(running.lines) == 4

    status, body = running.health()
    assert status == 200
    assert body["stage"] == "serving"
    assert body["fenced"] is False
    assert body["generation"] == 1
    assert body["workspace"] == str(running.state_dir.parent / "workspace")
    assert body["supervisor_pid"] == running.process.pid
    assert body["pid"] != running.process.pid
    assert body["launch_protocol"] == 1

    snapshot = running.status()
    assert snapshot["status"] == "ok"
    assert snapshot["endpoint"] == running.endpoint
    assert snapshot["pid"] == running.process.pid
    assert snapshot["generation"] == 1
    assert snapshot["stage"] == "serving"
    assert snapshot["active_upgrade"] is None
    assert snapshot["active_image"] == body["image_digest"]

    assert control_socket_path(running.state_dir).stat().st_mode & 0o777 == 0o600


def test_api_bearer_has_no_control_authority(running: _Running) -> None:
    status, body = running.post_control("/api/kernel/drain", running.token)
    assert status == 401
    assert body["code"] == "missing_control_credential"


def test_missing_api_bearer_is_refused(running: _Running) -> None:
    request = urllib.request.Request(f"{running.endpoint}/api/health")
    try:
        with _OPENER.open(request, timeout=10.0):
            raise AssertionError("health answered without a bearer token")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_sigterm_stops_the_child_and_removes_the_socket(running: _Running) -> None:
    child = running.child_pid()
    assert running.close(sig=signal.SIGTERM) == 0
    assert _dead(child, timeout=30.0)
    assert not control_socket_path(running.state_dir).exists()


def test_sigkill_of_the_supervisor_does_not_leave_an_orphan_child(running: _Running) -> None:
    child = running.child_pid()
    try:
        running.close(sig=signal.SIGKILL)
        # The child watches the supervisor pid it was launched with: it must exit on its own, never
        # keep the workspace lock after its supervisor is gone.
        assert _dead(child, timeout=30.0)
    finally:
        # SIGKILL runs no cleanup in the supervisor, so this test owns the stale socket it left.
        control_socket_path(running.state_dir).unlink(missing_ok=True)


def test_a_dash_leading_bearer_token_still_starts_the_child(tmp_path: Path) -> None:
    """A generated credential can begin with '-'; it must reach the child, not be read as a flag.

    ``secrets.token_urlsafe`` produces a leading dash about one time in 64, which previously made
    argparse exit 2 before the child started. The token here is that exact shape, deterministically.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    (workspace / "voidcode.json").write_text(
        json.dumps({"schema_version": 1, "model": {"model": None}, "policy": {}, "providers": {}}),
        encoding="utf-8",
    )
    dash_token = "-dash-leading-token-" + "x" * 24
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", _DASH_TOKEN_BOOT, str(workspace), str(state_dir), dash_token],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert process.stdout is not None
        lines: list[str] = []
        endpoint = ""
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    raise AssertionError(f"supervisor exited early: {process.stderr.read() if process.stderr else ''}")
                continue
            text = line.strip()
            lines.append(text)
            if text.startswith("kernel endpoint: "):
                endpoint = text.partition(": ")[2].strip()
            elif text.startswith("kernel ready: "):
                break
        else:
            raise AssertionError(f"supervisor did not become ready: {lines}")

        assert any(line == f"kernel bearer token: {dash_token}" for line in lines)
        request = urllib.request.Request(f"{endpoint}/api/health")
        request.add_header("authorization", f"Bearer {dash_token}")
        with _OPENER.open(request, timeout=10.0) as response:
            body = json.loads(response.read())
        assert body["stage"] == "serving"
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=90)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


_DASH_TOKEN_BOOT = """
import sys
from pathlib import Path
from voidcode.kernel.supervisor import run_supervisor

sys.exit(
    run_supervisor(
        workspace=Path(sys.argv[1]),
        state_dir=Path(sys.argv[2]),
        host="127.0.0.1",
        port=0,
        bearer_token=sys.argv[3],
        trusted_plugins=frozenset(),
        session_id=None,
        open_browser=False,
    )
)
"""


_OWNED_BOOT = """
import sys
from pathlib import Path
from voidcode.kernel.supervisor import run_supervisor

sys.exit(
    run_supervisor(
        workspace=Path(sys.argv[1]),
        state_dir=Path(sys.argv[2]),
        host="127.0.0.1",
        port=0,
        bearer_token=None,
        trusted_plugins=frozenset(),
        session_id=None,
        open_browser=False,
        owner_pid=int(sys.argv[3]),
    )
)
"""

#: An owner that spawns the supervisor as its own child, then dies on demand. The supervisor's
#: owner check is ``getppid() != owner_pid``, so the supervisor must really be parented by it.
_OWNER_BOOT = """
import os, subprocess, sys, time

child = subprocess.Popen([sys.executable, "-X", "utf8", "-c", sys.argv[1], sys.argv[2], sys.argv[3], str(os.getpid())])
sys.stderr.write("owner-child-pid: %d\\n" % child.pid)
sys.stderr.flush()
time.sleep(600)
"""


def test_owner_death_stops_the_supervisor_and_its_child(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    (workspace / "voidcode.json").write_text(
        json.dumps({"schema_version": 1, "model": {"model": None}, "policy": {}, "providers": {}}),
        encoding="utf-8",
    )
    # A real owner process, killed hard: its SIGKILL runs no cleanup, which is the case that would
    # otherwise leave the supervisor and its kernel child holding the workspace.
    owner = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", _OWNER_BOOT, _OWNED_BOOT, str(workspace), str(state_dir)],
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    supervisor_pid: int | None = None
    try:
        assert owner.stderr is not None
        line = owner.stderr.readline()
        assert line.startswith("owner-child-pid: "), line
        supervisor_pid = int(line.partition(": ")[2])

        deadline = time.monotonic() + _READY_TIMEOUT
        child_pid: int | None = None
        while time.monotonic() < deadline and child_pid is None:
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(5.0)
                try:
                    client.connect(str(control_socket_path(state_dir)))
                    client.sendall(b'{"op":"status"}\n')
                    buffer = b""
                    while b"\n" not in buffer:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                finally:
                    client.close()
                child_pid = json.loads(buffer.split(b"\n", 1)[0])["pid"]
            except OSError, ValueError, KeyError:
                time.sleep(0.25)
        assert child_pid is not None, "supervisor never served a child"

        owner.kill()
        owner.wait(timeout=30)

        # The owner's hard death must stop the supervisor (non-zero, no fallback spawn) and the
        # kernel child must exit too, so the workspace lock is released.
        assert supervisor_pid is not None
        assert _dead(supervisor_pid, timeout=60.0)
        assert _dead(child_pid, timeout=30.0)
        assert not control_socket_path(state_dir).exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        if supervisor_pid is not None and not _dead(supervisor_pid, timeout=2.0):
            os.kill(supervisor_pid, signal.SIGKILL)
            control_socket_path(state_dir).unlink(missing_ok=True)
        if owner.stderr is not None:
            owner.stderr.close()
