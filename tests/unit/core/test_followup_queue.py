"""Tests for Core follow-up persistent queueing and sequential delivery."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from tests.fixtures.core_factory import loopback_server
from tests.fixtures.reload_plugin import BLOCK_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _setup_core(tmp_path: Path, server: OpenAILoopbackServer) -> tuple[Core, Path]:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", blocking=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=tmp_path / "state")
    return core, workspace


def test_submitting_prompt_while_run_active_queues_without_altering_context(tmp_path: Path) -> None:
    replies = [
        _turn(_call("block-1", BLOCK_TOOL)),
        _turn(output="turn 1 done"),
        _turn(output="turn 2 done"),
    ]
    with loopback_server(replies) as server:
        core, workspace = _setup_core(tmp_path, server)
        session = core.create_session("sess-queue")
        session_id = session.session_id

        # Start Turn 1 in background
        t1 = threading.Thread(
            target=lambda: core.run("Turn 1 prompt", session_id=session_id, root_action_budget=3),
            daemon=True,
        )
        t1.start()

        # Wait until Turn 1 is blocked in BLOCK_TOOL
        marker = workspace / "block.started"
        deadline = time.monotonic() + 10
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.is_file()

        # Session is running
        sess_record = core.store.session(session_id)
        assert sess_record.status == "running"

        first_checkpoint = core.store.checkpoint(session_id)
        assert first_checkpoint is not None
        root_id = str(first_checkpoint.payload["root_id"])

        # Submit follow-up prompt while active
        queued_res = core.run("Turn 2 follow-up prompt", session_id=session_id)
        assert queued_res.status == "queued"
        assert queued_res.session_id == session_id

        # Verify continuation was staged in SQLite continuations table
        pending = core.store.pending_continuations(session_id, limit=10)
        core_pending = [c for c in pending if c.owner == "core"]
        assert len(core_pending) == 1
        assert core_pending[0].source == "user_message"
        assert core_pending[0].payload == {"prompt": "Turn 2 follow-up prompt", "root_action_budget": 3}
        assert core_pending[0].root_id == root_id

        # Verify Turn 1 context does NOT contain Turn 2 prompt
        events = core.store.events(session_id)
        user_events = [e for e in events if e.event_type == "core.user_message"]
        assert len(user_events) == 1
        assert user_events[0].payload["content"] == "Turn 1 prompt"

        # Unblock Turn 1 so it finishes and delivers Turn 2
        (workspace / "block.release").touch()
        t1.join(timeout=10)
        assert not t1.is_alive()

        # Verify Turn 2 was delivered as next turn sequentially
        events_after = core.store.events(session_id)
        user_events_after = [e for e in events_after if e.event_type == "core.user_message"]
        assert len(user_events_after) == 2
        assert user_events_after[0].payload["content"] == "Turn 1 prompt"
        assert user_events_after[1].payload["content"] == "Turn 2 follow-up prompt"

        # Continuation is completed
        assert core.store.continuation_status(core_pending[0].continuation_id) == "completed"
        assert core.store.root_budget_state(root_id) == (3, 3)
        assert len(server.requests) == 3
        core.close()


def test_interrupting_active_run_cancels_queued_followup(tmp_path: Path) -> None:
    replies = [
        _turn(_call("block-1", BLOCK_TOOL)),
    ]
    with loopback_server(replies) as server:
        core, workspace = _setup_core(tmp_path, server)
        session = core.create_session("sess-interrupt")
        session_id = session.session_id

        # Start Turn 1
        t1 = threading.Thread(target=lambda: core.run("Turn 1 prompt", session_id=session_id), daemon=True)
        t1.start()

        # Wait until Turn 1 is blocked
        marker = workspace / "block.started"
        deadline = time.monotonic() + 10
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.is_file()

        # Submit follow-up prompt while active
        queued_res = core.run("Discarded follow-up", session_id=session_id)
        assert queued_res.status == "queued"

        # Check pending in SQLite
        pending = core.store.pending_continuations(session_id, limit=10)
        core_pending = [c for c in pending if c.owner == "core"]
        assert len(core_pending) == 1
        cid = core_pending[0].continuation_id

        # Cancel session (Esc / user interrupt)
        cancelled = core.cancel_session(session_id, reason="user cancel")
        assert cancelled is True
        t1.join(timeout=10)

        # Continuation must be marked cancelled / discarded
        assert core.store.continuation_status(cid) == "cancelled"

        # No user_message for the follow-up was ever emitted
        events = core.store.events(session_id)
        user_events = [e for e in events if e.event_type == "core.user_message"]
        assert len(user_events) == 1
        assert user_events[0].payload["content"] == "Turn 1 prompt"
        core.close()
