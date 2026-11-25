"""Runtime wakeup behavior: parking/resume, startup recovery, fair tick, staged atomicity."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.core_factory import loopback_server, open_core_for_test
from tests.fixtures.s5_plugin import PLUGIN_ID as S5_PLUGIN_ID
from tests.fixtures.s5_plugin import WRITE_TOOL, write_s5_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import SessionStatus, StoredEvent, WakeupRecord

OBSERVER_PLUGIN_ID = "wakeup-probe"
STAGE_PLUGIN_ID = "stage-probe"
_OBSERVER_CODE = """
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.hooks import RunFinishedFact, WakeupFiredFact


def observe(fact, context, services):
    if isinstance(fact, WakeupFiredFact):
        services.stage_event("wakeup_pending", {
            "session_id": fact.session_id,
            "run_id": fact.run_id,
            "wakeup_id": fact.wakeup_id,
            "owner": fact.owner,
            "role": fact.role,
            "kind": fact.payload.get("kind"),
        })
    elif isinstance(fact, RunFinishedFact):
        services.stage_event("run_settled", {
            "session_id": fact.session_id,
            "run_id": fact.run_id,
            "status": fact.status,
        })


def create_plugin(config):
    return Plugin(observer=observe, observed_facts=("wakeup_fired", "run_finished"))
"""
_STAGE_CODE = """
from collections.abc import Mapping

from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolEffect


class Stage:
    definition = CommandDefinition(
        "probe.stage",
        "stage a wakeup",
        {
            "type": "object",
            "properties": {
                "due": {"type": "integer"},
                "key": {"type": ["string", "null"]},
                "cancel": {"type": ["string", "null"]},
                "raise_after": {"type": "boolean"},
            },
            "required": ["due"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation, services):
        args = invocation.arguments
        wakeup_id = services.stage_wakeup(
            {"text": "staged", "kind": "stage"},
            due_at_ms=int(args["due"]),
            idempotency_key=None if args.get("key") in (None, "") else str(args["key"]),
            role="user",
        )
        if args.get("cancel"):
            services.stage_cancel_wakeup(str(args["cancel"]))
        if args.get("raise_after"):
            raise RuntimeError("fixture failed after staging")
        services.stage_event("staged", {"wakeup_id": wakeup_id})
        return CommandResult(status="ok", data={"wakeup_id": wakeup_id})


def create_plugin(config):
    return Plugin(commands=(Stage(),))
"""


def _write_source(root: Path, code: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(code, encoding="utf-8")
    return root


def _configure(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    approval: str = "yolo",
    plugins: Mapping[str, object] | None = None,
) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_plugins: dict[str, object] = {"files": {"enabled": False}}
    config_plugins.update(plugins or {})
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": approval},
                "providers": {"endpoint": {"api_key": "loopback-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": config_plugins,
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": list(calls)}, finish="tool_calls" if calls else "stop")


def _events(core: Core, session_id: str, event_type: str) -> list[StoredEvent]:
    return [event for event in core.store.events(session_id) if event.event_type == event_type]


def _wakeup(core: Core, wakeup_id: str) -> WakeupRecord:
    with core.store._lock:
        row = core.store._connection.execute(
            "SELECT wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget, role, due_at, status, claim_run_id "
            "FROM wakeups WHERE wakeup_id = ?",
            (wakeup_id,),
        ).fetchone()
    assert row is not None
    return WakeupRecord(
        wakeup_id=str(row[0]),
        owner=str(row[1]),
        workspace=str(row[2]),
        session_id=str(row[3]),
        payload=json.loads(row[4]),
        root_id=str(row[5]),
        root_action_budget=None if row[6] is None else int(row[6]),
        role=str(row[7]),
        due_at=int(row[8]),
        status=str(row[9]),
        claim_run_id=None if row[10] is None else str(row[10]),
    )


def _pin_ticker(core: Core):
    real_tick = core.tick

    def _idle(now_ms: int | None = None, **_kwargs: object) -> dict[str, object]:
        return {"fired": None}

    core.tick = _idle  # type: ignore[method-assign]
    return real_tick


def test_parked_wakeup_keeps_its_claim_and_resume_lets_the_observer_settle(tmp_path: Path) -> None:
    source = write_s5_plugin(tmp_path / "s5")
    observer = _write_source(tmp_path / "observer", _OBSERVER_CODE)
    replies = [
        _turn(_call("write", WRITE_TOOL, action="write", path="target.txt", text="governed")),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        _configure(
            tmp_path,
            server,
            approval="ask",
            plugins={S5_PLUGIN_ID: {"path": str(source)}, OBSERVER_PLUGIN_ID: {"path": str(observer)}},
        )
        core = open_core_for_test(tmp_path)
        real_tick = _pin_ticker(core)
        session_id = core.create_session("sess").session_id
        due = int(time.time() * 1000)
        core.store.schedule_wakeup("wake-park", "cron-like", str(core.workspace), session_id, {"text": "write it", "kind": "cron"}, due, role="user")

        fired = real_tick()
        assert fired["fired"] == "wake-park"
        assert fired["wakeup_status"] == "claimed"
        assert fired["status"] == SessionStatus.WAITING
        session = core.store.session(session_id)
        assert session.status is SessionStatus.WAITING
        claim = _wakeup(core, "wake-park")
        assert claim.status == "claimed" and claim.claim_run_id == session.run_id
        claimed_run = claim.claim_run_id
        assert claimed_run is not None

        pending = _events(core, session_id, f"plugin.{OBSERVER_PLUGIN_ID}.wakeup_pending")
        assert len(pending) == 1
        pending_payload = dict(pending[0].payload)
        assert pending_payload["wakeup_id"] == "wake-park"
        assert pending_payload["run_id"] == claimed_run
        assert pending_payload["owner"] == "cron-like"
        assert pending_payload["role"] == "user"
        assert pending_payload["kind"] == "cron"

        request_id = str(_events(core, session_id, "core.approval_requested")[-1].payload["request_id"])
        resumed = core.resume(session_id, approval_request_id=request_id, decision="allow")
        assert resumed.status == "completed", resumed.error

        settled = _wakeup(core, "wake-park")
        assert settled.status == "completed"
        assert settled.claim_run_id == claimed_run
        assert core.store.session(session_id).run_id != claimed_run
        runs = _events(core, session_id, f"plugin.{OBSERVER_PLUGIN_ID}.run_settled")
        assert runs
        settled_payload = dict(runs[-1].payload)
        assert settled_payload["session_id"] == session_id
        assert settled_payload["run_id"] == core.store.session(session_id).run_id
        assert settled_payload["run_id"] != claimed_run
        assert settled_payload["status"] == "completed"


def test_open_core_recovers_a_claimed_wakeup_run_exactly_once(tmp_path: Path, monkeypatch) -> None:
    # The test takes the claim manually; a started ticker could race it between
    # scheduling and the explicit claim.
    monkeypatch.setattr(Core, "_start_background_work", lambda _self: None)
    with loopback_server([]) as server:
        _configure(tmp_path, server)
        with open_core(tmp_path / "workspace", state_dir=tmp_path / "state") as first:
            session_id = first.create_session("orphan").session_id
            first.store.schedule_wakeup("wake-orphan", "owner", str(first.workspace), session_id, {"text": "wake"}, 0)
            claim = first.store.claim_and_start_wakeup(
                str(first.workspace),
                0,
                run_id="orphan-run",
                logical_turn="orphan-turn",
                revision_id=first.revision_id,
                eligible_owners=(),
            )
            assert claim is not None and claim[0].wakeup_id == "wake-orphan"
            assert first.store.session(session_id).status is SessionStatus.RUNNING

        with open_core(tmp_path / "workspace", state_dir=tmp_path / "state") as recovered:
            session = recovered.store.session(session_id)
            assert session.status is SessionStatus.INTERRUPTED
            assert _wakeup(recovered, "wake-orphan").status == "failed"
            interrupted = _events(recovered, session_id, "core.run_interrupted")
            assert len(interrupted) == 1
            assert interrupted[0].run_id == "orphan-run"
            assert recovered.store.checkpoint(session_id) is not None
            checkpoint = recovered.store.checkpoint(session_id)
            assert checkpoint is not None and checkpoint.kind.value == "interrupted"

        with open_core(tmp_path / "workspace", state_dir=tmp_path / "state") as reopened:
            assert len(_events(reopened, session_id, "core.run_interrupted")) == 1
            assert _wakeup(reopened, "wake-orphan").status == "failed"


def test_ticker_keeps_firing_other_due_sessions_while_one_run_blocks(tmp_path: Path) -> None:
    slow_release = threading.Event()

    def _reply(request: Mapping[str, object], _attempt: int) -> dict[str, object] | None:
        if request.get("method") == "GET":
            return {"object": "list", "data": [{"id": "exact-model", "object": "model"}]}
        messages = request.get("messages")
        text = ""
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, Mapping) and message.get("role") == "user":
                    text = str(message.get("content", ""))
                    break
        if "slow" in text:
            assert slow_release.wait(15.0)
            return openai_response(message={"role": "assistant", "content": "slow done"}, finish="stop")
        return openai_response(message={"role": "assistant", "content": "fast done"}, finish="stop")

    with loopback_server([]) as server:
        server.before_reply = _reply
        _configure(tmp_path, server)
        core = open_core_for_test(tmp_path)
        slow = core.create_session("slow").session_id
        fast = core.create_session("fast").session_id
        core.store.schedule_wakeup("wake-slow", "owner", str(core.workspace), slow, {"text": "slow work"}, 0, role="user")
        core.store.schedule_wakeup("wake-fast", "owner", str(core.workspace), fast, {"text": "fast work"}, 0, role="user")
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if _events(core, slow, "core.wakeup_fired") and _events(core, fast, "core.wakeup_fired"):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the blocked head starved the second due session")
            while time.monotonic() < deadline:
                if core.store.session(fast).status is SessionStatus.COMPLETED:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the free session never completed beside the blocked run")
            assert core.store.session(slow).status is SessionStatus.RUNNING
            assert _wakeup(core, "wake-fast").status == "completed"
            assert _wakeup(core, "wake-slow").status == "claimed"
        finally:
            slow_release.set()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and core.store.session(slow).status is not SessionStatus.COMPLETED:
            time.sleep(0.05)
        assert core.store.session(slow).status is SessionStatus.COMPLETED
        assert _wakeup(core, "wake-slow").status == "completed"


def test_ticker_records_bounded_errors_and_keeps_ticking(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        _configure(tmp_path, server)
        core = open_core_for_test(tmp_path)

    calls: list[int] = []
    real_tick = core.tick

    def _boom(now_ms: int | None = None) -> dict[str, object]:
        calls.append(1)
        raise RuntimeError("x" * 10_000)

    core.tick = _boom  # type: ignore[method-assign]
    try:
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and len(calls) < 2:
            time.sleep(0.05)
        status = core.wakeup_tick_status()
        assert len(calls) >= 2
        assert status["error"] is not None
        assert len(str(status["error"])) <= 500
    finally:
        core.tick = real_tick  # type: ignore[method-assign]


def test_staged_wakeup_service_commits_and_rolls_back_with_the_command(tmp_path: Path) -> None:
    stage = _write_source(tmp_path / "stage", _STAGE_CODE)
    with loopback_server([]) as server:
        _configure(tmp_path, server, plugins={STAGE_PLUGIN_ID: {"path": str(stage)}})
        core = open_core_for_test(tmp_path)
        _pin_ticker(core)
        session_id = core.create_session("sess").session_id
        future_due = int(time.time() * 1000) + 3_600_000
        core.store.schedule_wakeup("old", STAGE_PLUGIN_ID, str(core.workspace), session_id, {"text": "old"}, future_due)
        revision = core.revision_id

        result = core.invoke_command(session_id, "probe.stage", {"due": future_due, "key": "first", "cancel": "old"}, expected_revision=revision)
        assert result.status == "ok", result.error
        old_record = _wakeup(core, "old")
        assert old_record.status == "cancelled", f"unexpected record: {old_record}"
        staged_id = str(result.data["wakeup_id"])
        assert _wakeup(core, staged_id).status == "pending"
        assert _events(core, session_id, f"plugin.{STAGE_PLUGIN_ID}.staged")

        staged_events_before = len(_events(core, session_id, f"plugin.{STAGE_PLUGIN_ID}.staged"))
        failed = core.invoke_command(
            session_id, "probe.stage", {"due": future_due, "key": "rollback", "raise_after": True}, expected_revision=revision
        )
        assert failed.status == "error"
        with core.store._lock:
            row = core.store._connection.execute(
                "SELECT wakeup_id FROM wakeups WHERE owner = ? AND payload_json LIKE '%staged%'",
                (STAGE_PLUGIN_ID,),
            ).fetchall()
        assert [item[0] for item in row] == [staged_id]
        assert len(_events(core, session_id, f"plugin.{STAGE_PLUGIN_ID}.staged")) == staged_events_before

        retry = core.invoke_command(session_id, "probe.stage", {"due": future_due, "key": "first"}, expected_revision=revision)
        assert retry.status == "ok" and retry.data["wakeup_id"] == staged_id
        with core.store._lock:
            count = core.store._connection.execute("SELECT COUNT(*) FROM wakeups WHERE wakeup_id = ?", (staged_id,)).fetchone()
        assert count is not None and count[0] == 1

        cancelled = core.invoke_command(
            session_id, "probe.stage", {"due": future_due, "key": "first", "cancel": staged_id}, expected_revision=revision
        )
        assert cancelled.status == "ok", cancelled.error
        assert _wakeup(core, staged_id).status == "cancelled"
        terminal_retry = core.invoke_command(session_id, "probe.stage", {"due": future_due, "key": "first"}, expected_revision=revision)
        assert terminal_retry.status == "error"
        assert "terminal status" in terminal_retry.error


def test_driver_failure_abandons_the_claim_and_records_one_interruption(tmp_path: Path, monkeypatch) -> None:
    # The test takes the claim manually; a started ticker could race it between
    # scheduling and the explicit claim.
    monkeypatch.setattr(Core, "_start_background_work", lambda _self: None)
    with loopback_server([]) as server:
        _configure(tmp_path, server)
        core = open_core_for_test(tmp_path)
        session_id = core.create_session("sess").session_id
        core.store.schedule_wakeup("wake-fail", "owner", str(core.workspace), session_id, {"text": "fail"}, 0, role="user")
        claim = core.store.claim_and_start_wakeup(
            str(core.workspace),
            0,
            run_id="run-fail",
            logical_turn="turn-fail",
            revision_id=core.revision_id,
            eligible_owners=(),
        )
        assert claim is not None
        wakeup, session = claim

        def _boom(_execution) -> None:
            raise RuntimeError("driver exploded")

        monkeypatch.setattr(core._turn_driver, "drive", _boom)
        core._drive_claimed_wakeup_safely(wakeup, session)

        assert core.store.session(session_id).status is SessionStatus.INTERRUPTED
        assert _wakeup(core, "wake-fail").status == "failed"
        interrupted = _events(core, session_id, "core.run_interrupted")
        assert len(interrupted) == 1
        assert interrupted[0].run_id == "run-fail"
        assert interrupted[0].payload["stop_cause"] == "error"
        assert "driver exploded" in str(interrupted[0].payload["reason"])


def test_fact_delivery_failure_abandons_the_claim_and_leaves_no_running_session(tmp_path: Path, monkeypatch) -> None:
    # The test takes the claim manually; a started ticker could race it between
    # scheduling and the explicit claim.
    monkeypatch.setattr(Core, "_start_background_work", lambda _self: None)
    with loopback_server([]) as server:
        _configure(tmp_path, server)
        core = open_core_for_test(tmp_path)
        session_id = core.create_session("sess").session_id
        core.store.schedule_wakeup("wake-delivery", "owner", str(core.workspace), session_id, {"text": "deliver"}, 0, role="user")
        claim = core.store.claim_and_start_wakeup(
            str(core.workspace),
            0,
            run_id="run-delivery",
            logical_turn="turn-delivery",
            revision_id=core.revision_id,
            eligible_owners=(),
        )
        assert claim is not None
        wakeup, session = claim

        def _boom(*_args, **_kwargs) -> None:
            raise RuntimeError("fact delivery exploded")

        monkeypatch.setattr(core._fact_delivery, "deliver", _boom)
        core._drive_claimed_wakeup_safely(wakeup, session)

        assert core.store.session(session_id).status is SessionStatus.INTERRUPTED
        assert _wakeup(core, "wake-delivery").status == "failed"
        interrupted = _events(core, session_id, "core.run_interrupted")
        assert len(interrupted) == 1
        assert "fact delivery exploded" in str(interrupted[0].payload["reason"])


def test_async_wakeup_dispatch_is_bounded_and_eventually_drains(tmp_path: Path, monkeypatch) -> None:
    from voidcode.core.engine._wakeups import MAX_ACTIVE_WAKEUP_RUNS

    # This test drives the ticker explicitly; never let the core's own ticker race
    # the setup by starting before the schedules exist.
    monkeypatch.setattr(Core, "_start_background_work", lambda _self: None)
    total = MAX_ACTIVE_WAKEUP_RUNS + 3
    with loopback_server([]) as server:
        _configure(tmp_path, server)
        core = open_core_for_test(tmp_path)
        real_tick = core.tick
        session_ids = [core.create_session(f"sess-{index}").session_id for index in range(total)]
        for index, session_id in enumerate(session_ids):
            core.store.schedule_wakeup(f"wake-{index}", "owner", str(core.workspace), session_id, {"text": "work"}, 0)

        started: list[str] = []
        release = threading.Event()

        def _fake_launch(wakeup, _session) -> None:
            started.append(wakeup.wakeup_id)
            worker = threading.Thread(target=lambda: release.wait(10.0), daemon=True)
            worker.start()
            with core._session_locks_guard:
                core._wakeup_workers.append(worker)

        core._launch_claimed_wakeup = _fake_launch  # type: ignore[method-assign]
        core._wakeup_tick_mode.async_dispatch = True
        try:
            first = real_tick()
            assert len(first["fired_ids"]) == MAX_ACTIVE_WAKEUP_RUNS  # type: ignore[arg-type]
            assert len(started) == MAX_ACTIVE_WAKEUP_RUNS

            second = real_tick()
            assert second["fired"] is None
            assert len(started) == MAX_ACTIVE_WAKEUP_RUNS

            release.set()
            deadline = time.monotonic() + 5.0
            while len(started) < total and time.monotonic() < deadline:
                real_tick()
                time.sleep(0.02)
            assert len(started) == total
        finally:
            release.set()
            core._wakeup_tick_mode.async_dispatch = False
