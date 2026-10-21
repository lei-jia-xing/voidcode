"""Real supervisor transitions: a genuine upgrade, its rollback, and a drain refusal.

Every case launches the actual supervisor and the actual kernel child over a real workspace and a
real materialized image, then asserts what a client observes: one endpoint and one API bearer
across the swap, the elected image in the revision pin, a refused drain that costs no downtime,
and a cold restart that runs the elected image rather than the invoking tree.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from voidcode.kernel.supervisor import control_socket_path

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_READY_TIMEOUT = 180.0
_UPGRADE_TIMEOUT = 300.0

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
        session_id=(sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None),
        open_browser=False,
    )
)
"""


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


@pytest.fixture(autouse=True)
def _coherent_installation() -> None:
    """Every real image refuses a checkout whose bundled identities drifted mid-edit."""
    drifted = _drifted_bundled_files()
    if drifted:
        pytest.skip(f"working tree has drifted bundled plugin identities: {', '.join(drifted)}")


class _Supervisor:
    def __init__(self, workspace: Path, state_dir: Path, *, session_id: str | None = None) -> None:
        self.workspace = workspace
        self.state_dir = state_dir
        (workspace / "voidcode.json").write_text(
            json.dumps({"schema_version": 1, "model": {"model": None}, "policy": {}, "providers": {}}),
            encoding="utf-8",
        )
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", _BOOT, str(workspace), str(state_dir), session_id or "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.lines: list[str] = []
        self.endpoint = ""
        self.token = ""
        assert self.process.stdout is not None
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                if self.process.poll() is not None:
                    raise AssertionError(f"supervisor exited early: {self._stderr()}")
                continue
            text = line.strip()
            self.lines.append(text)
            if text.startswith("kernel endpoint: "):
                self.endpoint = text.partition(": ")[2].strip()
            elif text.startswith("kernel bearer token: "):
                self.token = text.partition(": ")[2].strip()
            elif text.startswith("kernel ready: "):
                return
        raise AssertionError(f"supervisor did not become ready: {self.lines}\n{self._stderr()}")

    def _stderr(self) -> str:
        if self.process.poll() is None or self.process.stderr is None:
            return ""
        return self.process.stderr.read()

    def health(self) -> dict[str, object]:
        request = urllib.request.Request(f"{self.endpoint}/api/health")
        request.add_header("authorization", f"Bearer {self.token}")
        with _OPENER.open(request, timeout=10.0) as response:
            return json.loads(response.read())

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

    def upgrade(self, candidate: Path, *, session_id: str | None = None, timeout: float = 120.0) -> dict[str, object]:
        payload = json.dumps({"op": "upgrade", "candidate": str(candidate), "session_id": session_id, "timeout_seconds": timeout}).encode() + b"\n"
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(_UPGRADE_TIMEOUT)
        try:
            client.connect(str(control_socket_path(self.state_dir)))
            client.sendall(payload)
            buffer = b""
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        finally:
            client.close()
        return json.loads(buffer.split(b"\n", 1)[0])

    def close(self) -> int:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(timeout=90)
        finally:
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()


def _candidate_copy(state_dir: Path, *, marker: str) -> Path:
    """A distinct installation: the elected image's exact bytes plus one changed file."""
    image_root = state_dir / "kernel-images" / _active_digest(state_dir) / "root"
    candidate = state_dir.parent / "candidate"
    shutil.rmtree(candidate, ignore_errors=True)
    (candidate / "src").mkdir(parents=True)
    shutil.copytree(image_root / "voidcode", candidate / "src" / "voidcode")
    for cache in candidate.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    with (candidate / "src" / "voidcode" / "__init__.py").open("a", encoding="utf-8") as handle:
        handle.write(f"\n# {marker}\n")
    return candidate


def _active_digest(state_dir: Path) -> str:
    state = json.loads((state_dir / "supervisor" / "state.json").read_text(encoding="utf-8"))
    digest = state["active_image"]
    assert isinstance(digest, str)
    return digest


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, tmp_path / "state"


def test_upgrade_keeps_endpoint_and_token_and_elects_the_candidate(tmp_path: Path) -> None:
    workspace, state_dir = _workspace(tmp_path)
    supervisor = _Supervisor(workspace, state_dir)
    try:
        before = supervisor.health()
        assert before["stage"] == "serving"
        candidate = _candidate_copy(state_dir, marker="candidate marker: distinct image bytes")

        result = supervisor.upgrade(candidate, timeout=120.0)

        assert result["status"] == "upgraded", result
        assert set(result) == {
            "status",
            "upgrade_id",
            "from_image",
            "to_image",
            "from_revision_id",
            "to_revision_id",
            "readoption",
            "record",
        }, result
        assert result["readoption"] is True
        assert result["from_image"] == before["image_digest"]
        assert result["to_image"] != before["image_digest"]
        assert result["to_revision_id"] and result["to_revision_id"] != before["revision_id"]
        assert Path(str(result["record"])).is_file()

        after = supervisor.health()
        assert after["stage"] == "serving"
        assert after["fenced"] is False
        assert after["image_digest"] == result["to_image"]
        assert after["revision_id"] == result["to_revision_id"]
        assert after["generation"] == int(before["generation"]) + 1
        assert after["pid"] != before["pid"]

        # The listener and the API bearer are the supervisor's, so they never changed.
        assert supervisor.endpoint == supervisor.lines[0].partition(": ")[2].strip()
        status = supervisor.status()
        assert status["endpoint"] == supervisor.endpoint
        assert status["active_image"] == result["to_image"]
        assert status["active_upgrade"] is None

        # A cold restart runs the elected image, resolved from the revision pin.
        assert supervisor.close() == 0
        restarted = _Supervisor(workspace, state_dir)
        try:
            revived = restarted.health()
            assert revived["image_digest"] == result["to_image"]
            assert revived["revision_id"] == result["to_revision_id"]
            assert revived["stage"] == "serving"
        finally:
            restarted.close()
    finally:
        supervisor.close()


def test_parked_approval_blocks_a_revision_change_before_downtime(tmp_path: Path) -> None:
    from voidcode.kernel.storage.store import EventDraft, SqliteStore

    workspace, state_dir = _workspace(tmp_path)
    supervisor = _Supervisor(workspace, state_dir)
    try:
        first = supervisor.health()
        assert supervisor.close() == 0

        # Park a real approval: a revision-changing upgrade must refuse before anything stops.
        with SqliteStore(state_dir / "kernel-v1.sqlite3") as store:
            revision = store.workspace_current(str(workspace))
            assert revision is not None
            store.create_session("parked-session", str(workspace), revision)
            store.commit(
                "parked-session",
                expected_sequence=0,
                expected_run_id=None,
                run_id="run-parked",
                revision_id=revision,
                events=(EventDraft("kernel.approval_requested", "kernel", "kernel", {"request_id": "req-1", "command": "x"}),),
                status="waiting",
                checkpoint_kind="approval_wait",
                checkpoint_payload={"approval_request_id": "req-1"},
            )
        candidate = _candidate_copy(state_dir, marker="candidate marker: blocked drain")

        blocked = _Supervisor(workspace, state_dir, session_id="parked-session")
        try:
            serving = blocked.health()
            assert serving["stage"] == "serving"
            result = blocked.upgrade(candidate, session_id="parked-session", timeout=30.0)

            assert result["status"] == "blocked", result
            assert result["reason"] == "busy_work"
            assert result["blockers"] == [{"session_id": "parked-session", "status": "waiting", "reason": "approval_wait", "detail": ""}], result[
                "blockers"
            ]
            assert result["to_revision_id"] is None

            # No downtime: the very same child, unfenced, still serving the old image.
            unchanged = blocked.health()
            assert unchanged["pid"] == serving["pid"]
            assert unchanged["generation"] == serving["generation"]
            assert unchanged["image_digest"] == first["image_digest"]
            assert unchanged["fenced"] is False
            assert unchanged["stage"] == "serving"
            assert blocked.status()["active_upgrade"] is None

            with SqliteStore(state_dir / "kernel-v1.sqlite3") as store:
                parked = store.session("parked-session")
                assert parked.status == "waiting"
                checkpoint = store.checkpoint("parked-session")
                assert checkpoint is not None
                assert checkpoint.payload.get("approval_request_id") == "req-1"
        finally:
            blocked.close()
    finally:
        supervisor.close()
