"""Behavior tests for the upgrade transport surface: fence, health, control, restart frame.

Each case drives a real HTTP server over a real kernel: business writes refuse with 503 while
fenced and resume after promotion, control routes answer only to the control credential, health
reports the stable identity the supervisor and clients poll, and a follow stream ends with the
last sequence it actually delivered.
"""

from __future__ import annotations

import json
import queue
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from tests.fixtures.reload_plugin import write_reload_plugin
from voidcode.kernel.activation import bundled_sources
from voidcode.kernel.transport.http import create_kernel_app, create_kernel_app_with_factory

PLUGIN_ID = "reload-fixture"
API_TOKEN = "api-token-for-upgrade-transport-tests"
CONTROL_TOKEN = "control-token-for-upgrade-transport-tests"


def _configure(workspace: Path, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {name: {"enabled": False} for name in bundled_sources() if name != "providers"}
    plugins[PLUGIN_ID] = {"path": str(source), "enabled": True}
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": "http://127.0.0.1:1/v1"}},
                "plugins": plugins,
            }
        ),
        encoding="utf-8",
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def _serving(app: object) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 30
    base = f"http://127.0.0.1:{port}"
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError as exc:
            if time.monotonic() > deadline:
                raise TimeoutError("test server never opened its port") from exc
            time.sleep(0.05)
    try:
        yield base
    finally:
        server.should_exit = True
        worker.join(timeout=15)


def _request(base: str, method: str, path: str, *, token: str | None = None, body: object = None) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json(body: bytes) -> dict[str, object]:
    payload = json.loads(body.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture()
def _world(tmp_path: Path) -> tuple[Path, Path]:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    source = write_reload_plugin(tmp_path / "plugin")
    _configure(workspace, source)
    return workspace, state


def _commit_events(state: Path, session_id: str, count: int, *, start_sequence: int = 1) -> None:
    database = state / "kernel-v1.sqlite3"
    with sqlite3.connect(database) as raw:
        revision = raw.execute("SELECT active_revision_id FROM workspace_state").fetchone()[0]
        for offset in range(count):
            sequence = start_sequence + offset
            raw.execute(
                """
                INSERT INTO session_events (session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json)
                VALUES (?, ?, 'run-fixture', 'kernel.note', 'kernel', 'kernel', ?, '{}')
                """,
                (session_id, sequence, revision),
            )
        raw.execute(
            "UPDATE sessions SET last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
            (start_sequence + count - 1, int(time.time() * 1000), session_id),
        )
        raw.commit()


def _read_frames(base: str, path: str, token: str, sink: queue.Queue[object], *, timeout_seconds: float = 60.0) -> None:
    """Collect SSE frames until the server ends the stream or the timeout passes."""
    request = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"}, method="GET")
    deadline = time.monotonic() + timeout_seconds
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            buffer = b""
            while time.monotonic() < deadline:
                # read1 returns one TCP chunk at a time; read would wait for the full size or EOF.
                chunk = response.read1(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n\n" in buffer:
                    raw, _, buffer = buffer.partition(b"\n\n")
                    for line in raw.split(b"\n"):
                        if line.startswith(b"data: "):
                            sink.put(json.loads(line[len(b"data: ") :].decode("utf-8")))
    except Exception as exc:  # noqa: BLE001 - the sink records stream failures as data
        sink.put({"stream_error": f"{type(exc).__name__}: {exc}"})
    finally:
        sink.put(None)


def test_health_reports_identity_and_stage(_world: tuple[Path, Path]) -> None:
    workspace, state = _world
    app = create_kernel_app(workspace=workspace, state_dir=state, bearer_token=API_TOKEN)
    with _serving(app) as base:
        status, body = _request(base, "GET", "/api/health", token=API_TOKEN)
        assert status == 200
        health = _json(body)
        assert health["status"] == "ok"
        assert health["fenced"] is False and health["stage"] == "serving"
        assert isinstance(health["pid"], int) and health["pid"] > 0
        assert health["workspace"] == str(workspace)
        assert health["revision_id"]
        assert health["launch_protocol"] == 1
        assert health["kernel_version"] and health["image_digest"]
        assert health["supervisor_pid"] == __import__("os").getppid()
        assert health["generation"] == 1 and health["active_upgrade"] is None
        assert "web_bundle" in health

        status, _ = _request(base, "GET", "/api/health", token="wrong-token")
        assert status == 401


def test_fenced_app_refuses_writes_serves_reads_and_promotes(_world: tuple[Path, Path]) -> None:
    workspace, state = _world
    app = create_kernel_app(
        workspace=workspace,
        state_dir=state,
        bearer_token=API_TOKEN,
        fenced=True,
        control_token=CONTROL_TOKEN,
    )
    with _serving(app) as base:
        status, body = _request(base, "GET", "/api/health", token=API_TOKEN)
        assert status == 200
        assert _json(body)["fenced"] is True

        status, body = _request(base, "POST", "/api/sessions", token=API_TOKEN, body={})
        assert status == 503 and _json(body)["code"] == "upgrade_pending"

        status, body = _request(base, "POST", "/api/sessions", token=API_TOKEN, body={"session_id": "fenced-1"})
        assert status == 503

        status, body = _request(base, "GET", "/api/commands", token=API_TOKEN)
        assert status == 200 and _json(body)["revision_id"]

        status, body = _request(base, "POST", "/api/kernel/promote", token=CONTROL_TOKEN, body={})
        assert status == 200 and _json(body) == {"serving": True}

        status, body = _request(base, "GET", "/api/health", token=API_TOKEN)
        health = _json(body)
        assert status == 200 and health["fenced"] is False and health["stage"] == "serving"

        status, body = _request(base, "POST", "/api/sessions", token=API_TOKEN, body={"session_id": "after-promote"})
        assert status == 201 and _json(body)["session_id"] == "after-promote"


def test_control_routes_reject_the_api_bearer(_world: tuple[Path, Path]) -> None:
    workspace, state = _world
    app = create_kernel_app(
        workspace=workspace,
        state_dir=state,
        bearer_token=API_TOKEN,
        fenced=True,
        control_token=CONTROL_TOKEN,
    )
    with _serving(app) as base:
        for path in ("/api/kernel/drain", "/api/kernel/admit", "/api/kernel/adopt", "/api/kernel/promote"):
            status, body = _request(base, "POST", path, token=API_TOKEN, body={})
            assert status == 401 and _json(body)["code"] == "missing_control_credential", path
            status, _ = _request(base, "POST", path, token="wrong-control-token", body={})
            assert status == 401, path

        status, body = _request(base, "POST", "/api/kernel/drain", token=CONTROL_TOKEN, body={"timeout_seconds": 5})
        assert status == 200 and _json(body)["drained"] is True

        status, body = _request(base, "POST", "/api/kernel/admit", token=CONTROL_TOKEN, body={})
        assert status == 200 and _json(body) == {"admitting": True}


def test_adopt_on_a_current_generation_is_a_noop(_world: tuple[Path, Path]) -> None:
    workspace, state = _world
    app = create_kernel_app(
        workspace=workspace,
        state_dir=state,
        bearer_token=API_TOKEN,
        fenced=True,
        control_token=CONTROL_TOKEN,
    )
    with _serving(app) as base:
        status, body = _request(base, "POST", "/api/kernel/adopt", token=CONTROL_TOKEN, body={})
        assert status == 200
        assert _json(body) == {"adopted": False, "reason": "already_current"}


def test_promote_without_a_kernel_reports_unsupported_state(_world: tuple[Path, Path]) -> None:
    workspace, state = _world
    app = create_kernel_app_with_factory(
        kernel_factory=lambda: None,
        bearer_token=API_TOKEN,
        fenced=True,
        control_token=CONTROL_TOKEN,
        workspace=workspace,
        state_dir=state,
    )
    with _serving(app) as base:
        status, body = _request(base, "POST", "/api/kernel/promote", token=CONTROL_TOKEN, body={})
        assert status == 409 and _json(body)["code"] == "unsupported_state"

        status, body = _request(base, "GET", "/api/health", token=API_TOKEN)
        health = _json(body)
        assert status == 200 and health["fenced"] is True and health["stage"] == "validated"

        status, body = _request(base, "GET", "/api/commands", token=API_TOKEN)
        assert status == 503 and _json(body)["code"] == "upgrade_pending"


def test_follow_stream_ends_with_the_last_delivered_cursor(_world: tuple[Path, Path]) -> None:
    workspace, state = _world
    app = create_kernel_app(
        workspace=workspace,
        state_dir=state,
        bearer_token=API_TOKEN,
        fenced=True,
        control_token=CONTROL_TOKEN,
    )
    with _serving(app) as base:
        status, body = _request(base, "POST", "/api/kernel/admit", token=CONTROL_TOKEN, body={})
        assert status == 200
        status, body = _request(base, "POST", "/api/kernel/promote", token=CONTROL_TOKEN, body={})
        assert status == 200
        status, body = _request(base, "POST", "/api/sessions", token=API_TOKEN, body={"session_id": "stream-1"})
        assert status == 201
        _commit_events(state, "stream-1", 151)

        sink: queue.Queue[object] = queue.Queue()
        reader = threading.Thread(
            target=_read_frames,
            args=(base, "/api/sessions/stream-1/events?after_sequence=0&follow=true", API_TOKEN, sink),
            daemon=True,
        )
        reader.start()
        head = sink.get(timeout=30)
        assert isinstance(head, dict) and "events" in head
        assert [event["sequence"] for event in head["events"]] == list(range(1, 152))

        status, body = _request(base, "POST", "/api/kernel/drain", token=CONTROL_TOKEN, body={"timeout_seconds": 10})
        assert status == 200 and _json(body)["drained"] is True

        frames: list[object] = []
        while True:
            frame = sink.get(timeout=30)
            if frame is None:
                break
            frames.append(frame)
        reader.join(timeout=10)
        assert frames, "the stream must end with a restart frame after a successful drain"
        last = frames[-1]
        assert isinstance(last, dict) and last.get("kind") == "server_restart" and last.get("cursor") == 151
        delivered = [frame["event"]["sequence"] for frame in frames[:-1] if isinstance(frame, dict) and frame.get("kind") == "event"]
        assert delivered == [], "the head already delivered the backlog; follow must not duplicate it"
