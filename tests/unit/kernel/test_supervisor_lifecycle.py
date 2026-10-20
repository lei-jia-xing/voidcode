"""Supervisor boundaries: listener, election read, CLI socket, and refusal shapes.

Each case drives real objects — a real bound socket, a real SQLite database written by the real
store, a real materialized image of the running tree, and a real AF_UNIX server — so the assertions
are the behavior a client observes rather than a description of the implementation.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from voidcode.kernel import supervisor as supervisor_module
from voidcode.kernel.bootstrap import state_directory
from voidcode.kernel.images import IMAGES_DIR_NAME, materialize, running_package_root
from voidcode.kernel.storage.store import SqliteStore
from voidcode.kernel.supervisor import (
    SupervisorError,
    _bind_listener,
    _endpoint_for,
    _Supervisor,
    control_socket_path,
)


def _supervisor(workspace: Path, state_dir: Path) -> _Supervisor:
    state_dir.mkdir(parents=True, exist_ok=True)
    return _Supervisor(
        workspace=workspace,
        state_dir=state_dir,
        host="127.0.0.1",
        port=0,
        bearer_token=None,
        trusted_plugins=frozenset(),
        session_id=None,
        open_browser=False,
    )


def _pin_workspace(state_dir: Path, workspace: Path, digest: str) -> None:
    """Record one adopted revision pinning ``digest``, exactly as a real adoption would."""
    with SqliteStore(state_dir / "kernel-v1.sqlite3") as store:
        revision = store.put_revision({"image": {"digest": digest, "kernel_version": "0.0.0", "launch_protocol": 1}})
        store.record_workspace_revision(str(workspace), revision)


def test_control_socket_path_fits_the_real_default_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default durable state path is already longer than an AF_UNIX address can hold."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    default = state_directory(workspace)
    assert default.is_relative_to(Path.home() / ".local" / "state" / "voidcode")
    # The old location would not fit: that is the reported failure, not a synthetic one.
    assert len(os.fsencode(default / "supervisor.sock")) > 104
    path = control_socket_path(default)
    assert path.parent != default
    assert len(os.fsencode(path)) <= 104


def test_control_socket_paths_are_distinct_and_bounded_for_long_unicode_states(tmp_path: Path) -> None:
    base = tmp_path / ("long-" * 40)
    first = control_socket_path(base / "工作区-one")
    second = control_socket_path(base / "工作区-two")
    assert first != second
    assert first.parent == second.parent
    assert len(os.fsencode(first)) <= 104


def test_control_socket_path_canonicalizes_a_symlinked_state_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert control_socket_path(link) == control_socket_path(real)


def test_control_socket_path_ignores_ambient_runtime_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server and a later CLI must agree even when their TMPDIR/XDG_RUNTIME_DIR differ."""
    state_dir = tmp_path / "state"
    first = control_socket_path(state_dir)
    monkeypatch.setenv("TMPDIR", str(tmp_path / ("x" * 80)))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("y" * 80)))
    assert control_socket_path(state_dir) == first


def test_control_directory_refuses_an_insecure_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "voidcode-test"
    directory.mkdir()
    directory.chmod(0o755)
    monkeypatch.setattr(supervisor_module, "_control_directory", lambda: directory)
    with pytest.raises(SupervisorError) as refusal:
        supervisor_module._ensure_control_directory()
    assert refusal.value.reason == "control_dir_insecure"


def test_control_directory_refuses_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = tmp_path / "voidcode-test"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(supervisor_module, "_control_directory", lambda: link)
    with pytest.raises(SupervisorError) as refusal:
        supervisor_module._ensure_control_directory()
    assert refusal.value.reason == "control_dir_insecure"


def test_listener_binds_ephemeral_loopback_and_reports_its_own_address() -> None:
    listener = _bind_listener("127.0.0.1", 0)
    try:
        address, port = listener.getsockname()
        assert address == "127.0.0.1"
        assert port > 0
        assert _endpoint_for(address, port) == f"http://127.0.0.1:{port}"
        # A second bind to the resolved port is refused: the listener is really holding it.
        with pytest.raises(OSError):
            _bind_listener("127.0.0.1", port).close()
    finally:
        listener.close()


def test_listener_refuses_a_non_loopback_address() -> None:
    with pytest.raises(ValueError, match="loopback only"):
        _bind_listener("0.0.0.0", 0)


def test_ipv6_endpoint_is_bracketed() -> None:
    assert _endpoint_for("::1", 4711) == "http://[::1]:4711"


def test_startup_uses_the_elected_image_not_the_invoking_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    elected = materialize(running_package_root(), state_dir / IMAGES_DIR_NAME)
    _pin_workspace(state_dir, workspace, elected.digest)

    supervisor = _supervisor(workspace, state_dir)
    image = supervisor._startup_image()

    assert image.digest == elected.digest
    assert image.root == elected.root
    assert image.bundle == elected.bundle


def test_missing_elected_image_refuses_instead_of_running_other_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    _pin_workspace(state_dir, workspace, "0" * 64)

    supervisor = _supervisor(workspace, state_dir)
    with pytest.raises(SupervisorError) as refusal:
        supervisor._startup_image()

    assert refusal.value.reason == "elected_image_missing"
    assert "0" * 64 in refusal.value.detail


def test_unpinned_state_snapshots_the_running_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    with SqliteStore(state_dir / "kernel-v1.sqlite3") as store:
        revision = store.put_revision({"image": None})
        store.record_workspace_revision(str(workspace), revision)

    supervisor = _supervisor(workspace, state_dir)
    image = supervisor._startup_image()

    assert (state_dir / IMAGES_DIR_NAME / image.digest / "root").is_dir()


def _frame_exchange(supervisor: _Supervisor, payload: bytes, *, expected_lines: int = 1) -> list[dict[str, object]]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10.0)
    try:
        client.connect(str(supervisor._socket_path))
        client.sendall(payload)
        buffer = b""
        while buffer.count(b"\n") < expected_lines:
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer += chunk
    finally:
        client.close()
    return [json.loads(line) for line in buffer.splitlines() if line.strip()]


def test_socket_answers_status_and_rejects_malformed_frames(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = _supervisor(workspace, tmp_path / "state")
    supervisor._serve_unix()
    try:
        assert supervisor._socket_path.stat().st_mode & 0o777 == 0o600
        assert supervisor._socket_path.parent.stat().st_mode & 0o777 == 0o700
        assert supervisor._socket_path.parent == control_socket_path(tmp_path / "state").parent

        [status] = _frame_exchange(supervisor, b'{"op":"status"}\n')
        assert status["status"] == "ok"
        assert status["pid"] > 0
        assert status["generation"] == 0
        assert status["active_upgrade"] is None
        assert status["endpoint"] == ""

        [error] = _frame_exchange(supervisor, b"not json\n")
        assert error["status"] == "error"
        assert error["reason"] == "invalid_json"

        [unknown] = _frame_exchange(supervisor, b'{"op":"nope"}\n')
        assert unknown == {"status": "error", "reason": "unknown_op", "detail": "unsupported op 'nope'"}

        [oversized] = _frame_exchange(supervisor, b"x" * (supervisor_module.MAX_FRAME_BYTES + 1))
        assert oversized["status"] == "error"
        assert oversized["reason"] == "frame_too_large"
    finally:
        supervisor._shutdown()

    assert not supervisor._socket_path.exists()


def test_stop_frame_stops_the_supervisor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = _supervisor(workspace, tmp_path / "state")
    supervisor._serve_unix()
    try:
        [answer] = _frame_exchange(supervisor, b'{"op":"stop"}\n')
        assert answer == {"status": "stopping"}
        assert supervisor._stop.wait(5.0)
    finally:
        supervisor._shutdown()


def test_a_stale_control_socket_is_replaced(tmp_path: Path) -> None:
    """A socket file whose owner died is not a second supervisor; it is replaced on bind."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    supervisor = _supervisor(workspace, state_dir)
    path = control_socket_path(state_dir)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()

    supervisor._serve_unix()
    try:
        [status] = _frame_exchange(supervisor, b'{"op":"status"}\n')
        assert status["status"] == "ok"
    finally:
        supervisor._shutdown()
    assert not path.exists()


def test_a_second_supervisor_refuses_the_same_state_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    first = _supervisor(workspace, state_dir)
    first._serve_unix()
    try:
        second = _supervisor(workspace, state_dir)
        with pytest.raises(SupervisorError) as refusal:
            second._prepare()
        assert refusal.value.reason == "supervisor_running"
    finally:
        first._shutdown()
