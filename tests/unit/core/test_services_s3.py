"""S3 acceptance: typed messages, bounded event reads, governed queries, durable wakeups."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import SERVICES_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "s3 reasoning", "tool_calls": list(calls)},
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


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _messages(request: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = request.get("messages")
    assert isinstance(raw, list)
    return [message for message in raw if isinstance(message, Mapping)]


def _tool_result(core: Core, session_id: str, tool_name: str) -> str:
    for event in reversed(core.events_after(session_id)):
        if event.event_type != "core.tool_completed":
            continue
        call = event.payload.get("call")
        if not isinstance(call, Mapping) or call.get("tool_name") != tool_name:
            continue
        result = event.payload.get("result")
        if isinstance(result, Mapping):
            output = result.get("output")
            if isinstance(output, Mapping) and isinstance(output.get("text"), str):
                return str(output["text"])
            if result.get("error"):
                raise AssertionError(f"tool {tool_name} failed: {result['error']}")
    raise AssertionError(f"no completed result for {tool_name}")


def test_user_role_continuation_is_rendered_verbatim(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(_call("schedule-1", SERVICES_TOOL, action="wakeup", text="<system-reminder>finish the plan</system-reminder>")),
        _turn(output="scheduled"),
        # The wakeup's own turn sees the reminder as a user message.
        _turn(output="reminder acted on"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-wakeup")
        assert core.run("schedule a reminder", session_id="sess-wakeup").status == "completed"
        scheduled = json.loads(_tool_result(core, "sess-wakeup", SERVICES_TOOL))
        assert scheduled["wakeup_id"]

        fired = core.tick()
        assert fired["fired"] == scheduled["wakeup_id"]
        assert fired["wakeup_status"] == "completed"
        wakeup_request = server.requests[-1]
        rendered = _messages(wakeup_request)
        reminder_messages = [message for message in rendered if message.get("role") == "user"]
        assert reminder_messages, rendered
        reminder_texts = [str(message.get("content", "")) for message in reminder_messages]
        assert "<system-reminder>finish the plan</system-reminder>" in reminder_texts
        # The core did not re-wrap it as system prose.
        system_texts = [str(message.get("content", "")) for message in rendered if message.get("role") == "system"]
        assert not any("finish the plan" in text for text in system_texts)


def test_bounded_event_read_and_governed_model_query(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(_call("events-1", SERVICES_TOOL, action="events", limit=5, include_payload=False)),
        _turn(output="events read"),
        _turn(_call("query-1", SERVICES_TOOL, action="query", text="summarize the workspace")),
        # The governed query is a nested single dispatch: it consumes the next reply.
        _turn(output="a quiet answer"),
        _turn(output="query done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-read")
        assert core.run("read events", session_id="sess-read").status == "completed"
        views = json.loads(_tool_result(core, "sess-read", SERVICES_TOOL))
        assert views["count"] <= 5
        assert views["session"] == "sess-read"
        assert all(payload_present is False for payload_present in views["payloads"])

        assert core.run("ask the model", session_id="sess-read").status == "completed"
        queried = json.loads(_tool_result(core, "sess-read", SERVICES_TOOL))
        assert queried["answer"] == "a quiet answer"
        query_request = next(
            request for request in reversed(server.requests) if _messages(request) == [{"role": "user", "content": "summarize the workspace"}]
        )
        assert not query_request.get("tools")
        facts = [event for event in core.events_after("sess-read") if event.event_type == "core.model_query"]
        assert facts and facts[0].payload["model"] == "exact-model"

        scheduled_far = core.store.schedule_wakeup(
            "future", PLUGIN_ID, str(core.workspace), "sess-read", {"text": "later"}, int(time.time() * 1000) + 3_600_000
        )
        assert scheduled_far == "future"
        assert core.tick()["fired"] in (None, "future")
        assert all(record.due_at > int(time.time() * 1000) for record in core.store.pending_wakeups())


def test_wakeup_claim_is_atomic_and_never_replayed(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    with _server([_turn(output="unused")]) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-claim")
        now = int(time.time() * 1000)
        core.store.schedule_wakeup("due-now", PLUGIN_ID, str(core.workspace), "sess-claim", {"text": "fire"}, now - 1)

        def _tick() -> None:
            core.tick()

        workers = [threading.Thread(target=_tick) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        # Whoever claimed it (a caller or the core ticker), exactly one authentic fire exists.
        fired_facts = [
            event for session in core.sessions() for event in core.events_after(session.session_id) if event.event_type == "core.wakeup_fired"
        ]
        assert len(fired_facts) == 1
        assert core.store.pending_wakeups() == ()
        # Later ticks never replay the claimed wakeup.
        assert core.tick()["fired"] is None
