"""Lifecycle acceptance: session_start/session_end fire once per run and are recorded."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from tests.fixtures.core_factory import loopback_server, register_core
from tests.fixtures.reload_plugin import BLOCK_TOOL, PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.core.events import EventType

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "lifecycle reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _open(tmp_path: Path) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    core = open_core(workspace, state_dir=tmp_path / "state")
    register_core(core)
    return core


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path, *, approval_mode: str = "yolo") -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": approval_mode},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
            }
        ),
        encoding="utf-8",
    )


def _lifecycle_records(workspace: Path) -> list[str]:
    log_file = workspace / "lifecycle.log"
    if not log_file.exists():
        return []
    return [line.strip() for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hook_events(core: Core, session_id: str, kind: str) -> list:
    return [event for event in core.events_after(session_id) if event.event_type == EventType.HOOK_INVOKED and event.payload.get("kind") == kind]


def _run_to_failure(tmp_path: Path, source: Path) -> tuple[Core, object]:
    """Drive one run whose provider fails after session_start, then keep the core open."""
    workspace = tmp_path / "workspace"

    def configure(server: OpenAILoopbackServer) -> None:
        _configure(workspace, server, source)

    with loopback_server([]) as server:
        server.chat_status = 401
        server.replies.append({"error": {"message": "invalid api key", "code": "invalid_api_key"}})
        configure(server)
        core = _open(tmp_path)
        core.create_session("sess-failed")
        return core, core.run("fail after start", session_id="sess-failed")


def test_lifecycle_hooks_fire_once_per_run_with_durable_notes(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="done"), _turn(output="second run")]
    with loopback_server(replies) as server:
        workspace = tmp_path / "workspace"
        _configure(workspace, server, source)
        core = _open(tmp_path)
        core.create_session("sess-life")
        assert core.run("first", session_id="sess-life").status == "completed"

        records = _lifecycle_records(workspace)
        starts = [r for r in records if r.startswith("start:")]
        ends = [r for r in records if r.startswith("end:")]
        assert len(starts) == 1 and len(ends) == 1
        assert ends[0] == "end:completed"

        # The returned note is durable evidence, once per run and phase, with the
        # terminal status attached to the end record.
        start_events = _hook_events(core, "sess-life", "session_start")
        end_events = _hook_events(core, "sess-life", "session_end")
        assert len(start_events) == 1 and len(end_events) == 1
        assert start_events[0].payload["outcome"] == "noted"
        assert start_events[0].payload["note"] == f"started {start_events[0].run_id}"
        assert start_events[0].payload["status"] == ""
        assert end_events[0].payload["outcome"] == "noted"
        assert end_events[0].payload["status"] == "completed"
        assert end_events[0].payload["note"] == "ended completed"
        assert start_events[0].run_id == end_events[0].run_id

        # A second run fires the pair again, once each, for its own run identity.
        assert core.run("second", session_id="sess-life").status == "completed"
        records_again = _lifecycle_records(workspace)
        starts_again = [r for r in records_again if r.startswith("start:")]
        ends_again = [r for r in records_again if r.startswith("end:")]
        assert len(starts_again) == 2 and len(ends_again) == 2
        assert starts_again[0] != starts_again[1]
        per_run = {event.run_id for event in _hook_events(core, "sess-life", "session_start")}
        assert len(per_run) == 2


def test_lifecycle_hooks_do_not_duplicate_on_resume(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="stopped"), _turn(output="next answer")]
    with loopback_server(replies) as server:
        workspace = tmp_path / "workspace"
        _configure(workspace, server, source)
        core = _open(tmp_path)
        core.create_session("sess-resume")
        assert core.run("first", session_id="sess-resume").status == "completed"
        # Resuming the same run's session must not fire session_start a second time for that run.
        assert core.run("next", session_id="sess-resume").status == "completed"
        starts = [r for r in _lifecycle_records(workspace) if r.startswith("start:")]
        assert len(starts) == len(set(starts))
        # Exactly one durable start and one durable end per distinct run id.
        start_events = _hook_events(core, "sess-resume", "session_start")
        end_events = _hook_events(core, "sess-resume", "session_end")
        assert len(start_events) == len({event.run_id for event in start_events}) == 2
        assert len(end_events) == len({event.run_id for event in end_events}) == 2


def test_session_end_is_durable_on_user_stop(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", blocking=True, lifecycle_hooks=True)
    replies = [_turn(_call("block-1", BLOCK_TOOL))]
    with loopback_server(replies) as server:
        workspace = tmp_path / "workspace"
        _configure(workspace, server, source)
        core = _open(tmp_path)
        session = core.create_session("sess-stop")
        outcome: dict[str, object] = {}

        def _run() -> None:
            outcome["result"] = core.run("block until stopped", session_id=session.session_id)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        marker = workspace / "block.started"
        deadline = time.monotonic() + 20
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.is_file(), "the blocking tool never started"
        assert core.cancel_session(session.session_id, reason="user stopped the run") is True
        worker.join(timeout=20)
        assert not worker.is_alive(), "the cancelled run did not settle"

        result = outcome["result"]
        assert getattr(result, "status", None) == "interrupted"
        assert getattr(result, "stop_cause", None) == "user_stop"
        records = _lifecycle_records(workspace)
        assert [record for record in records if record.startswith("end:")] == ["end:interrupted"]

        start_events = _hook_events(core, "sess-stop", "session_start")
        end_events = _hook_events(core, "sess-stop", "session_end")
        assert len(start_events) == 1 and len(end_events) == 1
        assert end_events[0].payload["status"] == "interrupted"
        assert end_events[0].payload["outcome"] == "noted"
        assert end_events[0].payload["note"] == "ended interrupted"
        assert start_events[0].run_id == end_events[0].run_id == getattr(result, "run_id", None)


def test_session_end_is_recorded_once_when_the_run_fails(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    core, result = _run_to_failure(tmp_path, source)
    assert getattr(result, "status", None) == "failed"
    end_events = _hook_events(core, "sess-failed", "session_end")
    start_events = _hook_events(core, "sess-failed", "session_start")
    assert len(start_events) == 1 and len(end_events) == 1
    assert end_events[0].payload["status"] == "failed"
    assert end_events[0].payload["note"] == "ended failed"
    assert end_events[0].run_id == start_events[0].run_id


def test_session_end_is_durable_when_a_parked_run_is_cancelled(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL))]
    with loopback_server(replies) as server:
        workspace = tmp_path / "workspace"
        _configure(workspace, server, source, approval_mode="ask")
        core = _open(tmp_path)
        core.create_session("sess-parked")
        parked = core.run("write behind approval", session_id="sess-parked")
        assert parked.status == "waiting" and parked.pending_approval is not None

        # Start is already durable; a parked run has no end until it is actually stopped.
        starts = _hook_events(core, "sess-parked", "session_start")
        assert len(starts) == 1 and not _hook_events(core, "sess-parked", "session_end")

        assert core.cancel_session("sess-parked", reason="user cancelled parked run") is True
        assert core.session("sess-parked").status == "interrupted"
        ends = _hook_events(core, "sess-parked", "session_end")
        assert len(ends) == 1
        assert ends[0].payload["status"] == "interrupted"
        assert ends[0].payload["note"] == "ended interrupted"
        assert ends[0].run_id == starts[0].run_id == parked.run_id
