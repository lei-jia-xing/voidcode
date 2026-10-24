"""C5 acceptance: post-tool hooks run inside the governed invocation, not as a new one."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import (
    DEACTIVATE_TOOL,
    OVERLAY_COMMAND,
    PROBE_TOOL,
    write_reload_plugin,
)
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

PLUGIN_ID = "reload-fixture"
HOOK_NOTE = "hook note from reload_probe"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "hook reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
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


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path, *, read_only: bool = False) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": read_only},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _tool_messages(request: Mapping[str, object]) -> list[str]:
    messages = request.get("messages")
    assert isinstance(messages, list)
    return [str(message.get("content", "")) for message in messages if isinstance(message, Mapping) and message.get("role") == "tool"]


def test_post_tool_hook_note_lands_in_the_same_invocation(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", post_tool_hook=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="formatted")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-hook")

        assert core.run("write something", session_id="sess-hook").status == "completed"
        # The hook ran inside the invocation: the note is part of the tool result the model saw,
        # on the very next dispatch of the SAME run — not a new run, batch, or turn.
        assert len(server.requests) == 2
        tool_texts = _tool_messages(server.requests[1])
        assert any(HOOK_NOTE in text for text in tool_texts)
        # The durable event carries the same result shape the model read.
        completed = [event for event in core.events_after("sess-hook") if event.event_type == "core.tool_completed"]
        assert len(completed) == 1
        payload = json.dumps(dict(completed[0].payload), default=str)
        assert HOOK_NOTE in payload


def test_mutating_hook_is_skipped_under_a_restrictive_stance(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(
        source,
        tool_output="v1",
        post_tool_hook=True,
        post_tool_match=(DEACTIVATE_TOOL,),
        overlay_command=True,
    )
    # A read-tier tool still runs under the overlay; the mutating hook on it must not.
    replies = [_turn(_call("note-1", DEACTIVATE_TOOL)), _turn(output="done")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-hook-plan")
        entered = core.invoke_command("sess-hook-plan", OVERLAY_COMMAND, {"action": "on"}, expected_revision=core.revision_id)
        assert entered.status == "ok", entered.error

        assert core.run("read the state", session_id="sess-hook-plan").status == "completed"
        assert len(server.requests) == 2
        tool_texts = _tool_messages(server.requests[1])
        assert tool_texts and "skipped: read-only policy" in tool_texts[-1]
        assert HOOK_NOTE not in "\n".join(tool_texts)


def test_hook_lease_cannot_silently_drop_staged_state(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", post_tool_hook=True, post_tool_stage=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="done")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-hook-state")

        assert core.run("write something", session_id="sess-hook-state").status == "completed"
        # The hook's staging attempt is refused out loud and recorded on the result the model
        # reads: never a silent drop, and never durable state the hook did not own.
        tool_texts = _tool_messages(server.requests[1])
        assert tool_texts and "hook" in tool_texts[-1] and "cannot call" in tool_texts[-1]
        state = {row.owner: row for row in core.store.plugin_state_rows("session", "sess-hook-state")}
        # Only the tool's own write is there (note=v1): the refused hook write left no trace.
        assert state[PLUGIN_ID].payload.get("note") == "v1"
        assert "touched" not in state[PLUGIN_ID].payload
        event_types = [event.event_type for event in core.events_after("sess-hook-state")]
        assert not [kind for kind in event_types if kind.endswith("hook_note")]
