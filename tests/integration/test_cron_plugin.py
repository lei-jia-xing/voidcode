"""End-to-end tests for the cron plugin on the real core stack.

Drives the real core, plugin runtime, loopback provider and durable storage:
- A model tool call creates a durable session schedule armed with exactly one core wakeup
- The core ticker (driven explicitly here) fires that wakeup, and the fired run reaches the
  provider with the scheduled prompt as a verbatim USER message
- The terminal reschedule arms exactly one successor instant — no duplicates, no replay
- The `/loop` command creates a recurring schedule and fires it immediately

Timing: the plugin recomputes the next fire from the wall clock (legacy parity), so a tick is
only issued once its instant has genuinely passed — fabricating a future instant would settle a
fire against a clock that is still behind it. The core's own ticker is pinned for the same
reason: with a 1s cadence it would otherwise fire whenever the real clock crossed a due instant
and race every assertion below. Only *when* a tick happens is controlled; what a tick does
(claim one due wakeup, create its authentic run, drive it) is the real core path.

The plugin under test is the real bundled source, referenced by path so the host
imports the current code.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import StoredEvent, WakeupRecord
from voidcode.plugins.cron.loop_command import USAGE_MESSAGE

_CRON_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "cron"


def _cron_source(tmp_path: Path) -> Path:
    """The real cron plugin source."""
    _ = tmp_path
    return _CRON_PLUGIN_ROOT


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _core(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "ask"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            "cron": {"path": str(_cron_source(tmp_path)), "enabled": True},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _explicit_ticker(core: Core) -> Callable[[int | None], dict[str, object]]:
    """Pin the core's wall-clock ticker so fire instants are driven by the test."""
    real_tick = core.tick

    def _idle(now_ms: int | None = None) -> dict[str, object]:
        _ = now_ms
        return {"fired": None}

    core.tick = _idle  # type: ignore[method-assign]
    return real_tick


def _await_instant(due_ms: int, *, timeout_seconds: float = 10.0) -> None:
    """Wait until an instant has genuinely arrived, then let the core claim it."""
    deadline = time.monotonic() + timeout_seconds
    while int(time.time() * 1000) < due_ms:
        if time.monotonic() > deadline:
            pytest.fail(f"scheduled instant {due_ms} never arrived")
        time.sleep(0.01)


def _cron_call(call_id: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "cron_create", "arguments": json.dumps(arguments)},
    }


def _tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    return [
        str(entry["function"]["name"])
        for entry in tools
        if isinstance(entry, dict) and isinstance(entry.get("function"), dict)  # type: ignore[union-attr]
    ]


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request.get("messages")
    return [entry for entry in messages if isinstance(entry, dict)] if isinstance(messages, list) else []


def _user_texts(request: dict[str, object]) -> list[str]:
    return [str(message["content"]) for message in _messages(request) if message.get("role") == "user"]


def _fires(core: Core, session_id: str) -> list[StoredEvent]:
    return [event for event in core.events_after(session_id) if event.event_type == "kernel.wakeup_fired"]


def _plugin_events(core: Core, session_id: str, event_type: str) -> list[StoredEvent]:
    return [event for event in core.events_after(session_id) if event.event_type == f"plugin.cron.{event_type}"]


def _schedules(core: Core, session_id: str) -> dict[str, object]:
    """The session's stored schedules, or an empty mapping when this plugin wrote no state yet."""
    payload, _version = core.store.plugin_state("cron", "session", session_id)
    if not isinstance(payload, Mapping):
        return {}
    schedules = payload["schedules"]
    assert isinstance(schedules, Mapping)
    return dict(schedules)


def _pending(core: Core) -> list[WakeupRecord]:
    return list(core.store.pending_wakeups(str(core.workspace)))


def test_a_fired_schedule_reaches_the_provider_and_rearms_once(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_cron_call("call-cron-1", {"schedule": "every 1s", "prompt": "tick tock"})],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "scheduled"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "first tick"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "second tick"}, finish="stop"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        tick = _explicit_ticker(core)
        session_id = core.create_session().session_id

        result = core.run("schedule a tick", session_id=session_id)
        assert result.status == "completed"
        assert result.output == "scheduled"
        assert "cron_create" in _tool_names(server.requests[0])

        # The tool call really created one durable schedule armed with exactly one wakeup.
        schedules = _schedules(core, session_id)
        assert len(schedules) == 1
        schedule_id, entry = next(iter(schedules.items()))
        assert isinstance(entry, Mapping)
        assert entry["kind"] == "every"
        assert entry["prompt"] == "tick tock"
        assert entry["enabled"] is True
        assert entry["expires_at"] == entry["created_at"] + 7 * 86400 * 1000

        armed = _pending(core)
        assert [wakeup.wakeup_id for wakeup in armed] == [entry["wakeup_id"]]
        first = armed[0]
        assert first.session_id == session_id
        assert first.owner == "cron"
        assert first.role == "user"
        assert first.payload == {"kind": "cron", "text": "tick tock", "schedule_id": schedule_id}
        # A 1s cadence fires on the first second boundary after creation.
        assert first.due_at == (int(entry["created_at"]) // 1000 + 1) * 1000
        assert _fires(core, session_id) == []

        # Nothing fires before its instant.
        assert tick(now_ms=int(first.due_at) - 1) == {"fired": None}
        assert _fires(core, session_id) == []

        _await_instant(int(first.due_at))
        assert tick(now_ms=int(first.due_at))["fired"] == first.wakeup_id

        # Exactly one firing, committed on the owning session under this plugin's attribution.
        fires = _fires(core, session_id)
        assert len(fires) == 1
        assert fires[0].source == "plugin.cron"
        assert fires[0].payload["role"] == "user"
        assert fires[0].payload["payload"] == {"kind": "cron", "text": "tick tock", "schedule_id": schedule_id}

        # The fired run really dispatched to the provider with the scheduled prompt as a USER message.
        assert len(server.requests) == 3
        fired_request = server.requests[2]
        assert _user_texts(fired_request)[-1] == "tick tock"
        assert "tick tock" in json.dumps(_messages(fired_request), ensure_ascii=False)

        # The terminal reschedule armed exactly one successor for the same schedule.
        rearmed = _pending(core)
        assert len(rearmed) == 1
        assert rearmed[0].wakeup_id != first.wakeup_id
        assert rearmed[0].payload == {"kind": "cron", "text": "tick tock", "schedule_id": schedule_id}
        assert rearmed[0].due_at > first.due_at
        # Recomputed from completion time: a long run delays the next fire, missed slots never replay.
        assert rearmed[0].due_at <= int(time.time() * 1000) + 1000
        assert _schedules(core, session_id)[schedule_id]["wakeup_id"] == rearmed[0].wakeup_id
        settled = _plugin_events(core, session_id, "schedule_fired")
        assert [event.payload for event in settled] == [
            {"id": schedule_id, "outcome": "rearmed", "run_status": "completed", "next_fire_at": rearmed[0].due_at}
        ]

        # A later tick fires the successor exactly once and arms exactly one successor of its own.
        second = rearmed[0]
        _await_instant(int(second.due_at))
        assert tick(now_ms=int(second.due_at))["fired"] == second.wakeup_id
        assert len(_fires(core, session_id)) == 2
        assert len(server.requests) == 4
        assert _user_texts(server.requests[3])[-1] == "tick tock"

        successors = _pending(core)
        assert len(successors) == 1
        assert successors[0].due_at > second.due_at
        assert successors[0].wakeup_id not in {first.wakeup_id, second.wakeup_id}
        assert len({event.payload["wakeup_id"] for event in _fires(core, session_id)}) == 2


def test_loop_command_creates_a_schedule_and_fires_it_immediately(tmp_path: Path) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "loop ran"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "loop ran again"}, finish="stop"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        tick = _explicit_ticker(core)
        session_id = core.create_session().session_id

        result = core.invoke_command(
            session_id,
            "loop",
            {"input": "1s check the deploy"},
            expected_revision=core.command_catalog().revision_id,
        )

        assert result.status == "ok"
        schedule_id = str(result.data["schedule_id"])  # type: ignore[index]
        assert result.output == (
            f"Scheduled 'check the deploy' on cadence 'every 1s' (job ID: {schedule_id}). "
            "Recurring tasks auto-expire after 7 days. "
            f"To cancel sooner, use `cron_delete` or run `voidcode cron remove {schedule_id}`."
        )

        # Immediate arm: the wakeup is already due when the command returns.
        armed = _pending(core)
        assert len(armed) == 1
        assert armed[0].payload == {"kind": "cron", "text": "check the deploy", "schedule_id": schedule_id}
        assert armed[0].due_at <= int(time.time() * 1000)

        entry = _schedules(core, session_id)[schedule_id]
        assert isinstance(entry, Mapping)
        assert entry["kind"] == "every"
        assert entry["schedule_text"] == "every 1s"
        assert entry["expires_at"] == entry["created_at"] + 7 * 86400 * 1000

        assert tick(now_ms=int(armed[0].due_at))["fired"] == armed[0].wakeup_id
        assert len(_fires(core, session_id)) == 1
        assert _user_texts(server.requests[0])[-1] == "check the deploy"

        # The successor continues the `every 1s` cadence from the completion instant: strictly
        # after the fire that just settled, so the immediate iteration can never repeat itself.
        successors = _pending(core)
        assert len(successors) == 1
        assert successors[0].wakeup_id != armed[0].wakeup_id
        assert successors[0].due_at > armed[0].due_at


def test_loop_command_returns_usage_without_side_effects(tmp_path: Path) -> None:
    with _loopback_server([]) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id

        result = core.invoke_command(
            session_id,
            "loop",
            {"input": "   "},
            expected_revision=core.command_catalog().revision_id,
        )

        assert result.status == "ok"
        assert result.output == USAGE_MESSAGE
        assert core.store.pending_wakeups(str(core.workspace)) == ()
        assert _schedules(core, session_id) == {}
