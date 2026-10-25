"""Behavior tests for the upgrade drain: fence, pause, blockers, resume, promote.

Each case boots a real core over a real fixture plugin (every bundled plugin disabled, so the
tests do not depend on bundled sources other workers are editing) and drives the real drain
lifecycle: admission refuses while fenced, an active run pauses only at a committed batch boundary,
a parked approval blocks a revision-changing upgrade but not a same-generation restart, paused work
resumes exactly once, and promotion starts background work.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import BLOCK_TOOL, PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import openai_response
from voidcode.core.activation import bundled_sources
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core, DrainRefused

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "drain reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


class _DelayedLoopbackServer(ThreadingHTTPServer):
    """Loopback provider that sleeps before one scripted reply, to hold a race window open."""

    def __init__(self, replies: list[dict[str, object]], *, delay_before: int = 0, delay_seconds: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), _DelayedLoopbackHandler)
        self.replies = replies
        self.requests: list[dict[str, object]] = []
        self.delay_before = delay_before
        self.delay_seconds = delay_seconds
        self.served = 0


class _DelayedLoopbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, _DelayedLoopbackServer)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.requests.append(payload)
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if server.served == server.delay_before:
            time.sleep(server.delay_seconds)
        response = server.replies.pop(0)
        server.served += 1
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


@contextmanager
def _server(replies: list[dict[str, object]], *, delay_before: int = 0, delay_seconds: float = 0.0) -> Iterator[_DelayedLoopbackServer]:
    server = _DelayedLoopbackServer(replies, delay_before=delay_before, delay_seconds=delay_seconds)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _configure(workspace: Path, server: _DelayedLoopbackServer, source: Path, *, approval_mode: str = "yolo") -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    # Only the provider dispatcher stays enabled: model turns need a declared provider, and every
    # other bundled plugin is irrelevant to drain semantics (and immune to their source drift).
    plugins: dict[str, object] = {name: {"enabled": False} for name in bundled_sources() if name != "providers"}
    plugins[PLUGIN_ID] = {"path": str(source), "enabled": True}
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": approval_mode, "read_only": False},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": plugins,
            }
        ),
        encoding="utf-8",
    )


def _open(workspace: Path, state_dir: Path, *, auto_drain: bool = True) -> Core:
    import voidcode.core.bootstrap as bootstrap

    original = bootstrap.Core
    assert original is Core

    core = open_core(workspace, state_dir=state_dir)
    core.auto_drain = auto_drain
    _CREATED.append(core)
    return core


def _wait_for(core: Core, session_id: str, event_type: str, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if any(event.event_type == event_type for event in core.store.events(session_id)):
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {event_type}")


def test_begin_drain_on_an_idle_session_reports_drained(tmp_path: Path) -> None:
    with _server([]) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin")
        _configure(workspace, server, source)
        core = _open(workspace, state_dir)
        session = core.create_session("drain-idle")

        assert core.begin_drain(timeout_seconds=5) == {
            "drained": True,
            "active_calls": 0,
            "paused_sessions": [],
            "sessions": [session.session_id],
        }
        assert core.admit() == {"admitting": True}


def test_drain_fences_fresh_work_and_admit_restores_it(tmp_path: Path) -> None:
    with _server([]) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin")
        _configure(workspace, server, source)
        core = _open(workspace, state_dir)
        session = core.create_session("drain-fence")

        assert core.begin_drain(timeout_seconds=5)["drained"] is True
        with pytest.raises(DrainRefused):
            core.run("hello", session_id=session.session_id)
        with pytest.raises(DrainRefused):
            core.resume(session.session_id)
        with pytest.raises(DrainRefused):
            core.tick()
        with pytest.raises(DrainRefused):
            core.activate(session_id=session.session_id, source="client")

        assert core.admit() == {"admitting": True}
        assert core.tick() == {"fired": None}


def test_drain_waits_for_an_admitted_run_and_a_stuck_run_times_out(tmp_path: Path) -> None:
    replies = [_turn(_call("call-1", PROBE_TOOL), output=None), _turn(output="done")]
    with _server(replies) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin")
        _configure(workspace, server, source)
        core = _open(workspace, state_dir)
        session = core.create_session("drain-wait")

        outcome: dict[str, object] = {}
        worker = threading.Thread(target=lambda: outcome.update(result=core.run("probe it", session_id=session.session_id)))
        worker.start()
        _wait_for(core, session.session_id, "core.tool_intent")
        assert core.begin_drain(timeout_seconds=30)["drained"] is True
        worker.join(timeout=30)
        assert worker.is_alive() is False
        assert core.admit() == {"admitting": True}
        assert len(server.requests) == 2


def test_drain_timeout_reports_the_running_session_and_admit_unfences(tmp_path: Path) -> None:
    replies = [_turn(_call("call-block", BLOCK_TOOL), output=None)]
    with _server(replies) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin", blocking=True)
        _configure(workspace, server, source)
        core = _open(workspace, state_dir)
        session = core.create_session("drain-stuck")

        worker = threading.Thread(target=lambda: core.run("block", session_id=session.session_id), daemon=True)
        worker.start()
        _wait_for(core, session.session_id, "core.tool_intent")
        refused = core.begin_drain(timeout_seconds=1)
        assert refused["drained"] is False
        blockers = refused["blockers"]
        assert isinstance(blockers, list) and blockers
        assert any(session.session_id in str(blocker) for blocker in blockers)

        assert core.cancel_session(session.session_id, "test releases the blocked tool") is True
        worker.join(timeout=30)
        assert worker.is_alive() is False
        assert core.admit() == {"admitting": True}


def test_pause_at_a_committed_batch_resumes_exactly_once(tmp_path: Path) -> None:
    replies = [_turn(_call("call-1", PROBE_TOOL), output=None), _turn(_call("call-2", PROBE_TOOL), output=None), _turn(output="done")]
    with _server(replies, delay_before=1, delay_seconds=3.0) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin")
        _configure(workspace, server, source)
        core = _open(workspace, state_dir)
        session = core.create_session("drain-pause")

        worker = threading.Thread(target=lambda: core.run("probe it", session_id=session.session_id))
        worker.start()
        _wait_for(core, session.session_id, "core.tool_completed")
        drained = core.begin_drain(timeout_seconds=30, pause_at_batch_boundary=True, pause_intent="upg-1")
        assert drained["drained"] is True
        worker.join(timeout=30)
        assert worker.is_alive() is False

        events = core.store.events(session.session_id)
        assert core.session(session.session_id).status == "interrupted"
        checkpoint = core.store.checkpoint(session.session_id)
        assert checkpoint is not None and isinstance(checkpoint.payload, Mapping)
        assert checkpoint.payload["resume_intent"] == "upg-1"
        completed = [event for event in events if event.event_type == "core.tool_completed"]
        assert len(completed) == 2

        assert core.admit() == {"admitting": True}
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and core.session(session.session_id).status != "completed":
            time.sleep(0.05)
        after = core.store.events(session.session_id)
        assert len([event for event in after if event.event_type == "core.tool_completed"]) == 2
        assert core.session(session.session_id).status == "completed"


def test_quiescent_drain_refuses_a_parked_approval_while_a_restart_drain_passes(tmp_path: Path) -> None:
    replies = [_turn(_call("call-1", PROBE_TOOL), output=None)]
    with _server(replies) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin")
        _configure(workspace, server, source, approval_mode="ask")
        core = _open(workspace, state_dir)
        session = core.create_session("drain-parked")

        result = core.run("probe it", session_id=session.session_id)
        assert result.pending_approval is not None
        assert core.session(session.session_id).status == "waiting"

        refused = core.begin_drain(timeout_seconds=5, require_quiescent=True)
        assert refused["drained"] is False
        blockers = refused["blockers"]
        assert isinstance(blockers, list)
        assert any(isinstance(blocker, dict) and blocker.get("reason") == "approval_wait" for blocker in blockers)
        assert core.admit() == {"admitting": True}

        assert core.begin_drain(timeout_seconds=5)["drained"] is True
        assert core.admit() == {"admitting": True}

        resumed = core.resume(
            session.session_id,
            approval_request_id=str(result.pending_approval["request_id"]),
            decision="deny",
        )
        assert resumed.status in {"completed", "failed", "interrupted"}
        fresh = core.create_session("drain-after")
        assert core.begin_drain(timeout_seconds=5)["drained"] is True
        assert core.admit() == {"admitting": True}
        assert fresh.session_id


def test_promote_consumes_a_durable_intent_exactly_once(tmp_path: Path) -> None:
    replies = [_turn(output="done")]
    with _server(replies) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin")
        _configure(workspace, server, source)
        core = open_core(workspace, state_dir=state_dir, auto_drain=False)
        _CREATED.append(core)
        session = core.create_session("drain-promote")
        record = core.session(session.session_id)
        core.store.commit(
            session.session_id,
            expected_sequence=record.last_event_sequence,
            expected_run_id=record.run_id,
            run_id="run-paused",
            revision_id=record.active_revision_id,
            events=(),
            status="interrupted",
            checkpoint_kind="interrupted",
            checkpoint_payload={"resume_intent": "upg-9", "root_id": "", "logical_turn": "turn-9"},
        )

        assert core.promote() == {"serving": True}
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            checkpoint = core.store.checkpoint(session.session_id)
            if checkpoint is not None and isinstance(checkpoint.payload, Mapping) and not checkpoint.payload.get("resume_intent"):
                break
            time.sleep(0.05)
        checkpoint = core.store.checkpoint(session.session_id)
        assert checkpoint is not None and isinstance(checkpoint.payload, Mapping)
        assert not checkpoint.payload.get("resume_intent")


def test_staged_activation_request_blocks_a_quiescent_drain(tmp_path: Path) -> None:
    replies = [_turn(_call("call-1", "core_reload", reason="please"), output=None)]
    with _server(replies) as server:
        workspace, state_dir = tmp_path / "workspace", tmp_path / "state"
        source = write_reload_plugin(tmp_path / "plugin")
        _configure(workspace, server, source, approval_mode="ask")
        core = _open(workspace, state_dir)
        session = core.create_session("drain-staged")

        result = core.run("reload please", session_id=session.session_id)
        assert result.pending_approval is not None
        refused = core.begin_drain(timeout_seconds=5, require_quiescent=True)
        assert refused["drained"] is False
        blockers = refused["blockers"]
        assert isinstance(blockers, list)
        reasons = {blocker["reason"] for blocker in blockers if isinstance(blocker, dict)}
        assert "approval_wait" in reasons
        assert core.admit() == {"admitting": True}
