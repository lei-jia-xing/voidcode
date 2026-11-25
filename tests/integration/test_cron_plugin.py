"""End-to-end tests for the cron plugin on the real core stack.

Drives the real core, plugin runtime, loopback provider and durable storage:
- A model tool call creates a durable session schedule armed with exactly one core wakeup
- The core ticker (driven explicitly here) fires that wakeup, and the fired run reaches the
  provider with the scheduled prompt as a verbatim USER message
- The terminal reschedule arms exactly one successor instant — no duplicates, no replay
- The `/loop` command creates a recurring schedule and fires it immediately
- A parked approval keeps its claim and the resumed run settles the pending firing

Timing: recurrence starts at one minute (the documented minimum), so tests drive the core with
an explicit claim instant instead of sleeping. The core's own ticker is pinned so it cannot race
the explicit ticks. Only *when* a claim happens is controlled; what a tick does (claim due
wakeups, create their authentic run, drive it) is the real core path.

The plugin under test is the real bundled source, referenced by path so the host
imports the current code.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from tests.fixtures.core_factory import discard_core, loopback_server, register_core
from tests.fixtures.s5_plugin import PLUGIN_ID as S5_PLUGIN_ID
from tests.fixtures.s5_plugin import WRITE_TOOL, write_s5_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import StoredEvent, WakeupRecord
from voidcode.plugins.cron.loop_command import USAGE_MESSAGE

_CRON_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "cron"


def _cron_source(tmp_path: Path) -> Path:
    """The real cron plugin source."""
    _ = tmp_path
    return _CRON_PLUGIN_ROOT


def _plugin_clock(core: Core, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Pin the admitted cron callback's wall clock so recurrence needs no sleeping."""
    observer = next(
        (candidate for candidate, _facts, owner in core.plugin_runtime._observers if owner == "cron"),
        None,
    )
    if observer is None:
        raise AssertionError("the admitted cron observer is not registered")
    clock = {"now": int(time.time() * 1000)}
    monkeypatch.setitem(observer.__globals__, "_clock_ms", lambda: clock["now"])
    return clock


def _core(tmp_path: Path, server: OpenAILoopbackServer, *, extra_plugins: Mapping[str, object] | None = None) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {
        # The bundled files plugin is not under test here and is rewritten by another P4 lane.
        "files": {"enabled": False},
        "cron": {"path": str(_cron_source(tmp_path)), "enabled": True},
    }
    plugins.update(extra_plugins or {})
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
        "plugins": plugins,
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
    return core


def _explicit_ticker(core: Core) -> Callable[[int | None], dict[str, object]]:
    """Pin the core's wall-clock ticker so fire instants are driven by the test."""
    real_tick = core.tick

    def _idle(now_ms: int | None = None) -> dict[str, object]:
        _ = now_ms
        return {"fired": None}

    core.tick = _idle  # type: ignore[method-assign]
    return real_tick


def _tool_call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _cron_call(call_id: str, arguments: dict[str, object]) -> dict[str, object]:
    return _tool_call(call_id, "cron_create", arguments)


def _wakeup_status(core: Core, wakeup_id: str) -> str:
    with core.store._lock:
        row = core.store._connection.execute("SELECT status FROM wakeups WHERE wakeup_id = ?", (wakeup_id,)).fetchone()
    assert row is not None
    return str(row[0])


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
    return [event for event in core.events_after(session_id) if event.event_type == "core.wakeup_fired"]


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
    with core.store._lock:
        cursor = core.store._connection.execute(
            """
            SELECT wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget, role, due_at, status, claim_run_id
            FROM wakeups WHERE workspace = ? AND status = 'pending' ORDER BY due_at ASC
            """,
            (str(core.workspace),),
        )
        return [
            WakeupRecord(
                wakeup_id=str(row[0]),
                owner=str(row[1]),
                workspace=str(row[2]),
                session_id=str(row[3]),
                payload=json.loads(row[4]),
                root_id=str(row[5]),
                role=str(row[7]),
                due_at=int(row[8]),
                status=str(row[9]),
                claim_run_id=str(row[10]) if row[10] is not None else None,
                root_action_budget=None if row[6] is None else int(row[6]),
            )
            for row in cursor.fetchall()
        ]


def test_a_fired_schedule_reaches_the_provider_and_rearms_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_cron_call("call-cron-1", {"schedule": "every 1m", "prompt": "tick tock"})],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "scheduled"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "first tick"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "second tick"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        clock = _plugin_clock(core, monkeypatch)
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
        # Recurring schedules expire 7 days after creation: within one tick of now + 7d.
        now_ms = int(time.time() * 1000)
        assert abs(int(entry["expires_at"]) - (now_ms + 7 * 86400 * 1000)) < 60_000

        armed = _pending(core)
        assert [wakeup.wakeup_id for wakeup in armed] == [entry["wakeup_id"]]
        first = armed[0]
        assert first.session_id == session_id
        assert first.owner == "cron"
        assert first.role == "user"
        assert first.payload == {"kind": "cron", "text": "tick tock", "schedule_id": schedule_id}
        # A 1m cadence fires on the first minute boundary after creation.
        assert first.due_at == (int(entry["next_fire_at"]) // 1000) * 1000
        assert first.due_at >= int(entry["expires_at"]) - 7 * 86400 * 1000
        assert first.due_at < int(entry["expires_at"])
        assert _fires(core, session_id) == []

        # Nothing fires before its instant.
        assert tick(now_ms=int(first.due_at) - 1) == {"fired": None}
        assert _fires(core, session_id) == []

        clock["now"] = int(first.due_at)
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
        assert rearmed[0].due_at == first.due_at + 60_000
        assert _schedules(core, session_id)[schedule_id]["wakeup_id"] == rearmed[0].wakeup_id
        assert _schedules(core, session_id)[schedule_id]["pending"] is None
        settled = _plugin_events(core, session_id, "schedule_fired")
        assert [event.payload for event in settled] == [
            {
                "id": schedule_id,
                "wakeup_id": first.wakeup_id,
                "fired_sequence": fires[0].sequence,
                "outcome": "rearmed",
                "run_status": "completed",
                "next_fire_at": rearmed[0].due_at,
            }
        ]
        state_payload, _version = core.store.plugin_state("cron", "session", session_id)
        assert isinstance(state_payload, Mapping)
        finished_sequence = max(event.sequence for event in core.events_after(session_id) if event.event_type == "core.run_completed")
        assert int(state_payload["facts_cursor"]) >= finished_sequence

        # A later tick fires the successor exactly once and arms exactly one successor of its own.
        second = rearmed[0]
        clock["now"] = int(second.due_at)
        assert tick(now_ms=int(second.due_at))["fired"] == second.wakeup_id
        assert len(_fires(core, session_id)) == 2
        assert len(server.requests) == 4
        assert _user_texts(server.requests[3])[-1] == "tick tock"

        successors = _pending(core)
        assert len(successors) == 1
        assert successors[0].wakeup_id not in {first.wakeup_id, second.wakeup_id}
        assert len({event.payload["wakeup_id"] for event in _fires(core, session_id)}) == 2


def test_loop_command_creates_a_schedule_and_fires_it_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "loop ran"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "loop ran again"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        clock = _plugin_clock(core, monkeypatch)
        tick = _explicit_ticker(core)
        session_id = core.create_session().session_id

        result = core.invoke_command(
            session_id,
            "loop",
            {"input": "1m check the deploy"},
            expected_revision=core.command_catalog().revision_id,
        )

        assert result.status == "ok"
        schedule_id = str(result.data["schedule_id"])  # type: ignore[index]
        assert result.output == (
            f"Scheduled 'check the deploy' on cadence 'every 1m' (job ID: {schedule_id}). "
            "Recurring tasks auto-expire after 7 days. "
            f"To cancel sooner, use /loop delete {schedule_id}. "
            "A run already in flight is not interrupted."
        )

        # Immediate arm: the wakeup is already due when the command returns.
        armed = _pending(core)
        assert len(armed) == 1
        assert armed[0].payload == {"kind": "cron", "text": "check the deploy", "schedule_id": schedule_id}
        assert armed[0].due_at <= int(time.time() * 1000)

        entry = _schedules(core, session_id)[schedule_id]
        assert isinstance(entry, Mapping)
        assert entry["kind"] == "every"
        assert entry["schedule_text"] == "every 1m"
        now_ms = int(time.time() * 1000)
        assert abs(int(entry["expires_at"]) - (now_ms + 7 * 86400 * 1000)) < 60_000

        clock["now"] = int(armed[0].due_at)
        assert tick(now_ms=int(armed[0].due_at))["fired"] == armed[0].wakeup_id
        assert len(_fires(core, session_id)) == 1
        assert _user_texts(server.requests[0])[-1] == "check the deploy"

        # The successor is a fresh wakeup; the immediate iteration is never replayed.
        successors = _pending(core)
        assert len(successors) == 1
        assert successors[0].wakeup_id != armed[0].wakeup_id
        assert successors[0].due_at > armed[0].due_at


def test_parked_approval_keeps_the_claim_and_resume_settles_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = write_s5_plugin(tmp_path / "s5")
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call(
                        "call-write-1",
                        WRITE_TOOL,
                        {"action": "write", "path": "target.txt", "text": "governed"},
                    )
                ],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "approved write"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, extra_plugins={S5_PLUGIN_ID: {"path": str(source)}})
        clock = _plugin_clock(core, monkeypatch)
        tick = _explicit_ticker(core)
        session_id = core.create_session().session_id
        result = core.invoke_command(
            session_id,
            "loop",
            {"input": "1m write the governed file"},
            expected_revision=core.command_catalog().revision_id,
        )
        assert result.status == "ok", result.error
        schedule_id = str(result.data["schedule_id"])  # type: ignore[index]
        armed = _pending(core)
        assert len(armed) == 1

        clock["now"] = int(armed[0].due_at)
        fired = tick(now_ms=int(armed[0].due_at))
        assert fired["fired"] == armed[0].wakeup_id
        assert str(fired["status"]) == "waiting"
        assert fired["wakeup_status"] == "claimed"
        assert _wakeup_status(core, armed[0].wakeup_id) == "claimed"

        pending_entry = _schedules(core, session_id)[schedule_id]
        assert isinstance(pending_entry, Mapping)
        assert pending_entry["pending"] is not None
        assert pending_entry["next_fire_at"] is None
        assert pending_entry["wakeup_id"] == armed[0].wakeup_id

        approval = next(event for event in reversed(core.events_after(session_id)) if event.event_type == "core.approval_requested")
        resumed = core.resume(session_id, approval_request_id=str(approval.payload["request_id"]), decision="allow")
        assert resumed.status == "completed", resumed.error
        assert _wakeup_status(core, armed[0].wakeup_id) == "completed"

        settled = _schedules(core, session_id)[schedule_id]
        assert isinstance(settled, Mapping)
        assert settled["pending"] is None
        assert settled["wakeup_id"] != armed[0].wakeup_id
        assert int(settled["next_fire_at"]) > int(armed[0].due_at)
        successors = _pending(core)
        assert [wakeup.wakeup_id for wakeup in successors] == [settled["wakeup_id"]]
        events = [event.payload for event in _plugin_events(core, session_id, "schedule_fired")]
        assert events == [
            {
                "outcome": "rearmed",
                "id": schedule_id,
                "wakeup_id": armed[0].wakeup_id,
                "fired_sequence": _fires(core, session_id)[0].sequence,
                "run_status": "completed",
                "next_fire_at": settled["next_fire_at"],
            }
        ]
        state_payload, _version = core.store.plugin_state("cron", "session", session_id)
        assert isinstance(state_payload, Mapping)
        finished_sequence = max(event.sequence for event in core.events_after(session_id) if event.event_type == "core.run_completed")
        assert int(state_payload["facts_cursor"]) >= finished_sequence


def test_loop_command_returns_usage_without_side_effects(tmp_path: Path) -> None:
    with loopback_server([]) as server:
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
        assert _pending(core) == []
        assert _schedules(core, session_id) == {}


def test_stored_v1_state_refuses_startup_until_the_cron_row_is_reset(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session("legacy").session_id
        old_payload = json.dumps(
            {
                "schema_version": 1,
                "schedules": {},
                "last_finished_sequence": 0,
                "cursor_sequence": 0,
            }
        )
        with core.store._lock:
            core.store._connection.execute(
                """
                INSERT INTO plugin_state (
                    scope, scope_key, owner, version, schema_version, revision_id, payload_json, updated_at
                ) VALUES ('session', ?, 'cron', 1, 1, ?, ?, ?)
                """,
                (session_id, core.revision_id, old_payload, int(time.time() * 1000)),
            )
        core.close()
        discard_core(core)

        with pytest.raises(ValueError, match="plugin cron state schema mismatch: stored v1, declared v2"):
            open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
