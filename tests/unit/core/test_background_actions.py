"""Core regressions for owner background actions and idempotent child spawn.

Background actions are plugin-owned drivers claimed from their own table.
Each driver `invoke` commits under a fresh lease, so progress is durable action by
action; the driver never takes the session run lock, so the foreground stays usable.
Keyed spawns map (parent, owner, key) to one child: retries return it, differing
specs refuse, and a full budget still re-fetches it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.polling import wait_for
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.core.engine import Core
from voidcode.core.events import EventType
from voidcode.core.storage.store import EventDraft

PLUGIN_ID = "background-fixture"

_PLUGIN_CODE = '''
"""Fixture: enqueue/status/cancel background actions; the driver checkpoints per invoke."""

from __future__ import annotations

import time

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolEffect


class EnqueueCommand:
    definition = CommandDefinition(
        name="bg_enqueue",
        description="Enqueue one background action.",
        argument_schema={
            "type": "object",
            "properties": {"note": {"type": "string"}, "key": {"type": "string"}, "spins": {"type": "integer"}},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation, services):
        note = invocation.arguments.get("note", "")
        key = invocation.arguments.get("key")
        spins = invocation.arguments.get("spins", 2)
        action_id = services.enqueue_background_action(
            {"note": note, "spins": spins, "error": "business-noise-not-an-execution-error"},
            idempotency_key=key if isinstance(key, str) else None,
        )
        return CommandResult(status="ok", output=action_id, data={"action_id": action_id})


class StatusCommand:
    definition = CommandDefinition(
        name="bg_status",
        description="Read one background action status.",
        argument_schema={
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, invocation, services):
        status = services.background_action_status(str(invocation.arguments["action_id"]))
        if status is None:
            return CommandResult(status="error", error="unknown action")
        return CommandResult(status="ok", output=status.status, data=status.as_payload())


class CancelCommand:
    definition = CommandDefinition(
        name="bg_cancel",
        description="Cancel one background action.",
        argument_schema={
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation, services):
        done = services.cancel_background_action(str(invocation.arguments["action_id"]))
        return CommandResult(status="ok", output=str(done), data={"cancelled": done})


def _read_state(services):
    state, version = services.get_state("session")
    return (dict(state) if state else {"marks": []}), version


def drive(context):
    payload = dict(context.payload)
    note = payload.get("note", "")
    spins = int(payload.get("spins", 2))

    def first(_ctx, services):
        state, version = _read_state(services)
        state["marks"] = [*state.get("marks", []), f"first:{note}"]
        services.replace_state(state, expected_version=version)
        return "first"

    context.invoke(first)
    if note == "spawn-after-delay":
        time.sleep(float(payload.get("delay_seconds", 0.6)))

        def _spawn(_ctx, services):
            return services.spawn_child("child under captured background root", label="background-origin-child")

        context.invoke(_spawn)
        return
    if note == "raise":
        raise ValueError("boom from driver")
    if note == "raise-base":
        raise SystemExit("base exit from driver")
    for index in range(spins):
        if context.abort_signal.is_set():
            return
        time.sleep(0.2)

        def _mark(_ctx, services, _i=index):
            state, version = _read_state(services)
            state["marks"] = [*state.get("marks", []), f"mark-{_i}:{note}"]
            services.replace_state(state, expected_version=version)
            return f"mark-{_i}"

        context.invoke(_mark)


def create_plugin(config):
    _ = config
    return Plugin(
        commands=(EnqueueCommand(), StatusCommand(), CancelCommand()),
        background_action=drive,
    )
'''


def _write_plugin(source: Path) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(_PLUGIN_CODE, encoding="utf-8")


def _turn(output: str | None = None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": []}, finish="stop")


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


def _open(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "voidcode.json"
    if not config_path.exists():
        source = tmp_path / "plugin"
        _write_plugin(source)
        config = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": "yolo", "read_only": False},
            "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _enqueue(core: Core, session: str, **arguments: object) -> str:
    result = core.invoke_command(session, "bg_enqueue", dict(arguments), expected_revision=core.revision_id)
    assert result.status == "ok", result
    assert isinstance(result.data, Mapping)
    return str(result.data["action_id"])


def _status(core: Core, session: str, action_id: str) -> dict[str, object]:
    result = core.invoke_command(session, "bg_status", {"action_id": action_id}, expected_revision=core.revision_id)
    assert result.status == "ok", result
    assert isinstance(result.data, Mapping)
    return dict(result.data)


def _cancel(core: Core, session: str, action_id: str) -> bool:
    result = core.invoke_command(session, "bg_cancel", {"action_id": action_id}, expected_revision=core.revision_id)
    assert result.status == "ok", result
    assert isinstance(result.data, Mapping)
    return bool(result.data["cancelled"])


def _marks(core: Core, session: str) -> list[str]:
    state, _version = core.store.plugin_state(PLUGIN_ID, "session", session)
    if state is None:
        return []
    marks = state.get("marks", [])
    assert isinstance(marks, (list, tuple)) and all(isinstance(mark, str) for mark in marks)
    return list(marks)


def _claimed(core: Core, action_id: str) -> bool:
    record = core.store.background_action(action_id)
    return record is not None and record.status == "claimed"


def test_driver_checkpoints_are_durable_invoke_by_invoke(tmp_path: Path) -> None:
    """Each driver invoke commits: the first mark is readable before the action completes."""
    with _server([_turn(output="unused")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", note="hello", spins=6)
        wait_for(lambda: _marks(core, "parent") == ["first:hello"], what="the first checkpoint commit")
        # The action is still running its later invokes while the first mark is durable.
        assert _status(core, "parent", action_id)["status"] == "running"
        assert core.wait_background_actions("parent", timeout_seconds=20) == ()
        assert _status(core, "parent", action_id)["status"] == "completed"
        assert _marks(core, "parent") == ["first:hello", *[f"mark-{i}:hello" for i in range(6)]]


def test_cancel_reaches_a_live_driver_and_keeps_earlier_checkpoints(tmp_path: Path) -> None:
    """Cancelling a claimed action signals the driver; committed marks survive."""
    with _server([_turn(output="unused")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", note="slow", spins=50)
        wait_for(lambda: _status(core, "parent", action_id)["status"] == "running", what="the claim")
        wait_for(lambda: "first:slow" in _marks(core, "parent"), what="the first checkpoint commit")
        assert _cancel(core, "parent", action_id) is True
        wait_for(lambda: _status(core, "parent", action_id)["status"] == "cancelled", what="the cancel settle")
        assert _marks(core, "parent")[0] == "first:slow"
        assert core.wait_background_actions("parent", timeout_seconds=5) == ()


def test_stale_claim_reports_interrupted_and_blocks_nothing(tmp_path: Path) -> None:
    """A claimed row with no live driver is interrupted, and wait/foreground ignore it."""
    with _server([_turn(output="after stale")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        doomed = _enqueue(core, "parent", note="doomed", spins=50)
        wait_for(lambda: _claimed(core, doomed), what="the doomed claim")
        wait_for(lambda: "first:doomed" in _marks(core, "parent"), what="the doomed first checkpoint commit")
        # Simulate a crashed holder: orphan the claimed row with a foreign identity while the
        # live driver is signalled away, so the row stands alone with no driver in this process.
        with core.store._lock:  # noqa: SLF001 - the test simulates a dead claim holder
            core.store._connection.execute(
                "UPDATE background_actions SET status='claimed', claim_json=? WHERE action_id=?",
                (json.dumps({"pid": 99999999, "core": "dead"}), doomed),
            )
            core.store._connection.commit()
        abort = core._background.abort_event(doomed)
        if abort is not None:
            abort.set()
        wait_for(
            lambda: _status(core, "parent", doomed)["status"] in ("cancelled", "interrupted"),
            what="the doomed settle",
        )
        settled = core.wait_background_actions("parent", timeout_seconds=5)
        assert all(entry["status"] == "interrupted" or entry["action_id"] != doomed for entry in settled)
        assert core.run("after stale", session_id="parent", max_steps=4).status == "completed"
        # No replay: the driver marked once and never ran again.
        assert _marks(core, "parent").count("first:doomed") == 1


def test_keyed_spawn_returns_one_child_and_refuses_a_differing_spec(tmp_path: Path) -> None:
    """Same parent/owner/key/spec re-fetches one child; a differing spec is refused."""
    with _server([_turn(output="child done")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        first = core.spawn_child("parent", "child work", owner=PLUGIN_ID, idempotency_key="node-a")
        assert core.spawn_child("parent", "child work", owner=PLUGIN_ID, idempotency_key="node-a") == first
        assert len(core.store.descendant_sessions("parent")) == 1
        with pytest.raises(ValueError, match="different spec"):
            core.spawn_child("parent", "DIFFERENT work", owner=PLUGIN_ID, idempotency_key="node-a")
        assert core.find_keyed_child("parent", PLUGIN_ID, "node-a") == first
        assert core.find_keyed_child("parent", PLUGIN_ID, "missing") is None
        assert core.find_keyed_child("parent", "other-owner", "node-a") is None
        assert core.join_child("parent", first, timeout_seconds=30).status == "completed"


def test_keyed_retry_on_notify_less_spawn_fact_fails_loudly(tmp_path: Path) -> None:
    """A spawn fact without notify_on_completion is corrupt: KeyError, never silent repair."""
    with _server([_turn(output="child done")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        child = core.spawn_child("parent", "child work", owner=PLUGIN_ID, idempotency_key="node-repair")
        facts = [
            e.payload for e in core.store.events("parent") if e.event_type == EventType.CHILD_SPAWNED and e.payload.get("child_session_id") == child
        ]
        assert len(facts) == 1
        keyless = dict(facts[0])
        del keyless["notify_on_completion"]
        before = len(core.store.events("parent"))
        core._commit_control_event("parent", EventDraft(EventType.CHILD_SPAWNED, "core", "plugin", keyless))
        assert len(core.store.events("parent")) == before + 1
        with pytest.raises(KeyError, match="notify_on_completion"):
            core.spawn_child("parent", "child work", owner=PLUGIN_ID, idempotency_key="node-repair")
        assert len(core.store.events("parent")) == before + 1, "no repair commit appended"


def test_delayed_background_spawn_keeps_its_original_root_authorization(tmp_path: Path) -> None:
    with _server([_turn(output="first root done"), _turn(output="new unlimited root done")]) as server:
        core = _open(tmp_path, server)
        first = core.run("first root", session_id="parent", root_action_budget=1)
        assert first.status == "completed", first.error
        checkpoint = core.store.checkpoint("parent")
        assert checkpoint is not None
        original_root_id = str(checkpoint.payload["root_id"])
        assert core.store.root_budget_state(original_root_id) == (1, 1)

        action_id = "delayed-origin-action"
        core.store.enqueue_background_action(
            action_id,
            PLUGIN_ID,
            "parent",
            core.revision_id,
            {"note": "spawn-after-delay", "delay_seconds": 3.0},
            root_id=original_root_id,
            root_action_budget=1,
        )
        core._background.notify()  # noqa: SLF001 - seed the durable queue directly to isolate recovery.
        wait_for(lambda: "first:spawn-after-delay" in _marks(core, "parent"), what="background delay point")

        second = core.run("new root", session_id="parent")
        assert second.status == "completed", second.error
        second_checkpoint = core.store.checkpoint("parent")
        assert second_checkpoint is not None
        new_root_id = str(second_checkpoint.payload["root_id"])
        assert new_root_id != original_root_id

        wait_for(lambda: bool(core.list_descendants("parent")), what="delayed child reservation")
        child = core.list_descendants("parent")[0]
        wait_for(lambda: core.session(child.session_id).status == "interrupted", what="old-root child exhaustion")
        birth = core._delegation._birth_record(child.session_id)  # noqa: SLF001 - assert durable authorization at birth.
        assert birth is not None
        assert birth["root_id"] == original_root_id
        assert birth["root_action_budget"] == 1
        assert core.store.root_budget_state(original_root_id) == (1, 1)
        wait_for(
            lambda: core.store.background_action(action_id) is not None and core.store.background_action(action_id).status == "completed",
            what="background spawn completion",
        )
        assert core.store.root_budget_state(new_root_id) is None
        assert len(server.requests) == 2


def test_full_budget_still_refetches_a_keyed_child(tmp_path: Path) -> None:
    """At budget, a fresh key is refused but an existing key re-fetches without counting again."""
    with _server([_turn(output=f"child-{i}") for i in range(4)]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        assert core.config.max_root_children == 4
        for index in range(4):
            core.spawn_child("parent", f"work {index}", owner=PLUGIN_ID, idempotency_key=f"fill-{index}")
        assert len(core.store.descendant_sessions("parent")) == 4
        assert core.spawn_child("parent", "work 0", owner=PLUGIN_ID, idempotency_key="fill-0") == core.find_keyed_child("parent", PLUGIN_ID, "fill-0")
        # Full budget never bypasses spec verification for a mapped key.
        with pytest.raises(ValueError, match="different spec"):
            core.spawn_child("parent", "DIFFERENT work 0", owner=PLUGIN_ID, idempotency_key="fill-0")
        with pytest.raises(ValueError, match="budget exhausted"):
            core.spawn_child("parent", "one too many", owner=PLUGIN_ID, idempotency_key="fill-9")
        assert len(core.store.descendant_sessions("parent")) == 4


def test_message_and_action_share_one_key_without_swallowing(tmp_path: Path) -> None:
    """One owner/session/key names a model message AND a background action independently."""
    with _server([_turn(output="message turn")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        core.store.enqueue_continuation(
            "msg-ns-1",
            PLUGIN_ID,
            "parent",
            "plugin.bg",
            "run-x",
            core.revision_id,
            {"kind": "background_action", "text": "ordinary message with a colliding kind"},
            idempotency_key="shared-key",
            root_id="parent-root",
        )
        action_id = _enqueue(core, "parent", note="ns", key="shared-key", spins=0)
        assert action_id != "msg-ns-1"
        assert core.store.continuation_id_for_idempotency("parent", PLUGIN_ID, "shared-key") == "msg-ns-1"
        assert core.store.background_action_id_for_idempotency("parent", PLUGIN_ID, "shared-key") == action_id
        drained = core.drain_continuations("parent", limit=1)
        drained_ids = [row.continuation_id for row in drained]
        if "msg-ns-1" not in drained_ids:
            remaining = [row.continuation_id for row in core.store.pending_continuations("parent", limit=10)]
            assert "msg-ns-1" not in remaining, "the ordinary message must still drain as a model turn"
        assert core.wait_background_actions("parent", timeout_seconds=20) == ()
        assert _status(core, "parent", action_id)["status"] == "completed"


def test_foreground_run_succeeds_while_an_action_is_claimed(tmp_path: Path) -> None:
    """A claimed background action never takes the session run lock from a model turn."""
    with _server([_turn(output="foreground answer")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", note="blocker", spins=50)
        wait_for(lambda: _status(core, "parent", action_id)["status"] == "running", what="the blocker claim")
        assert core.run("foreground during claim", session_id="parent", max_steps=4).output == "foreground answer"
        assert _cancel(core, "parent", action_id) is True
        wait_for(lambda: _status(core, "parent", action_id)["status"] == "cancelled", what="the blocker settle")


def test_close_parks_live_drivers_without_replay(tmp_path: Path) -> None:
    """Closing the core aborts live drivers; the rows settle cancelled, never rerun."""
    with _server([]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", note="parked", spins=50)
        wait_for(lambda: _status(core, "parent", action_id)["status"] == "running", what="the parked claim")
        wait_for(lambda: "first:parked" in _marks(core, "parent"), what="the parked first checkpoint commit")
        core.close()
        _CREATED.remove(core)
        # A reopened core reports the settled row and never replays the driver.
        reopened = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
        _CREATED.append(reopened)
        record = reopened.store.background_action(action_id)
        assert record is not None and record.status == "cancelled"
        assert _marks(reopened, "parent")[0] == "first:parked"
        assert _marks(reopened, "parent").count("first:parked") == 1


def test_wait_raises_on_a_failed_action(tmp_path: Path) -> None:
    """A failed action makes the waiter raise with its id and error instead of masking it."""
    with _server([]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", note="raise", spins=0)
        with pytest.raises(RuntimeError, match=action_id) as excinfo:
            core.wait_background_actions("parent", action_id=action_id, timeout_seconds=20)
        assert "boom from driver" in str(excinfo.value)
        assert _status(core, "parent", action_id) == {"action_id": action_id, "status": "failed", "error": "ValueError: boom from driver"}


def test_base_exception_driver_exit_settles_and_clears_liveness(tmp_path: Path) -> None:
    """A SystemExit escaping the driver settles failed; the dead thread is never stuck running."""
    with _server([]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", note="raise-base", spins=0)
        with pytest.raises(RuntimeError, match=action_id) as excinfo:
            core.wait_background_actions("parent", action_id=action_id, timeout_seconds=20)
        assert "SystemExit: base exit from driver" in str(excinfo.value)
        assert _status(core, "parent", action_id) == {"action_id": action_id, "status": "failed", "error": "SystemExit: base exit from driver"}
        assert not core._background.is_live(action_id)


def test_business_error_field_is_never_misread_as_execution_error(tmp_path: Path) -> None:
    """Every fixture payload carries a business `error` key; completed rows report no error."""
    with _server([]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        action_id = _enqueue(core, "parent", note="ok", spins=0)
        assert core.wait_background_actions("parent", action_id=action_id, timeout_seconds=20) == ({"action_id": action_id, "status": "completed"},)
        assert _status(core, "parent", action_id) == {"action_id": action_id, "status": "completed", "error": None}
        record = core.store.background_action(action_id)
        assert record is not None
        assert record.payload["action"]["error"] == "business-noise-not-an-execution-error"


def test_wait_names_one_receipt_even_after_it_settled(tmp_path: Path) -> None:
    """An explicit wait checks its receipt's terminal state; a pre-settled failure still raises."""
    with _server([]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        failed_id = _enqueue(core, "parent", note="raise", spins=0)
        wait_for(lambda: _status(core, "parent", failed_id)["status"] == "failed", what="the failed settle")
        # The failure settled before the wait starts: the explicit receipt still surfaces it.
        with pytest.raises(RuntimeError, match=failed_id) as excinfo:
            core.wait_background_actions("parent", action_id=failed_id, timeout_seconds=5)
        assert "boom from driver" in str(excinfo.value)
        # A completed receipt reports success; an unknown or foreign id is refused visibly.
        done_id = _enqueue(core, "parent", note="done", spins=0)
        assert core.wait_background_actions("parent", action_id=done_id, timeout_seconds=20) == ({"action_id": done_id, "status": "completed"},)
        with pytest.raises(ValueError, match="does not belong"):
            core.wait_background_actions("parent", action_id="no-such-action", timeout_seconds=1)
        core.create_session("other")
        with pytest.raises(ValueError, match="does not belong"):
            core.wait_background_actions("other", action_id=done_id, timeout_seconds=1)


def test_crash_between_child_row_and_parent_fact_stays_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash after the child row but before the parent fact keeps the mapping verifiable.

    The birth record commits atomically with the row, so the lookup still finds the
    child, a differing spec is still refused, and a same-spec retry repairs exactly one
    child instead of spawning a second.
    """
    with _server([_turn(output="repaired child done")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        original_commit = core.commit_public_fact
        gate = {"armed": True}

        def _crash_once(session_id: str, event: EventDraft, **kwargs: object) -> object:
            if gate["armed"] and event.event_type == EventType.CHILD_SPAWNED:
                gate["armed"] = False
                raise RuntimeError("injected crash before the parent spawn fact")
            return original_commit(session_id, event, **kwargs)

        monkeypatch.setattr(core, "commit_public_fact", _crash_once)
        with pytest.raises(RuntimeError, match="injected crash"):
            core.spawn_child("parent", "crash-window work", owner=PLUGIN_ID, idempotency_key="crash-node")
        monkeypatch.setattr(core, "commit_public_fact", original_commit)
        assert len(core.store.descendant_sessions("parent")) == 1
        found = core.find_keyed_child("parent", PLUGIN_ID, "crash-node")
        assert found is not None
        # The birth record is core bookkeeping: no public fact, transcript, or history block.
        from voidcode.core.facts import fact_for_event
        from voidcode.core.history_renderer import render_event_block
        from voidcode.core.transcript import transcript_entry

        birth = core.store.events(found, limit=1)[0]
        assert (birth.sequence, birth.event_type) == (1, "core.child_reserved")
        assert fact_for_event(birth, ()) is None
        assert transcript_entry(birth) is None
        assert render_event_block(birth) is None
        assert len(core.store.descendant_sessions("parent")) == 1
        with pytest.raises(ValueError, match="different spec"):
            core.spawn_child("parent", "DIFFERENT work", owner=PLUGIN_ID, idempotency_key="crash-node")
        assert core.spawn_child("parent", "crash-window work", owner=PLUGIN_ID, idempotency_key="crash-node") == found
        assert len(core.store.descendant_sessions("parent")) == 1
        assert core.join_child("parent", found, timeout_seconds=30).status == "completed"


def test_cancel_before_first_run_is_never_started_late(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepted cancel of a reserved child claims it; no late driver may start it."""
    with _server([_turn(output="must never run")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        started: list[str] = []
        original_start = core._delegation._start_child_run

        def _hold(parent_id: str, child_id: str, *args: object) -> None:
            started.append(child_id)

        monkeypatch.setattr(core._delegation, "_start_child_run", _hold)
        child = core.spawn_child("parent", "reserved work", owner=PLUGIN_ID, idempotency_key="reserved-node")
        monkeypatch.setattr(core._delegation, "_start_child_run", original_start)
        assert started == [child]
        assert core.cancel_child("parent", child, reason="cancel before start") is True
        assert core.join_child("parent", child, timeout_seconds=10).status == "cancelled"
        # The late driver thread from the spawn races in now: it must refuse to start.
        core._delegation._run_child("parent", child, "reserved work", 32, "", False)
        assert [e.event_type for e in core.store.events(child) if e.event_type == "core.user_message"] == []
        assert len(server.requests) == 0
        # An explicit follow-up afterwards is still a legal fresh run.
        receipt = core.send_child("parent", child, "explicit follow-up")
        assert receipt.delivery == "started"
        assert core.join_child("parent", child, timeout_seconds=30).status == "completed"


def test_concurrent_same_key_spawns_start_exactly_one_initial_run(tmp_path: Path) -> None:
    """Two threads racing one key get one child id and trigger exactly one model turn."""
    with _server([_turn(output="one winner")]) as server:
        core = _open(tmp_path, server)
        core.create_session("parent")
        gate = threading.Barrier(2, timeout=30)
        original_run_child = core._delegation._run_child
        entered: list[str] = []

        def _gated_run_child(parent_id: str, child_id: str, *args: object) -> None:
            entered.append(child_id)
            return original_run_child(parent_id, child_id, *args)  # type: ignore[arg-type]

        core._delegation._run_child = _gated_run_child  # type: ignore[method-assign]
        results: dict[str, str] = {}
        start = threading.Barrier(3, timeout=30)

        def _contend(name: str) -> None:
            start.wait()
            try:
                results[name] = core.spawn_child("parent", "raced work", owner=PLUGIN_ID, idempotency_key="race-node")
            finally:
                gate.wait()

        threads = [threading.Thread(target=_contend, args=(name,)) for name in ("a", "b")]
        try:
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            core._delegation._run_child = original_run_child  # type: ignore[method-assign]
        assert not [thread for thread in threads if thread.is_alive()]
        assert len(results) == 2
        assert set(results.values()) == {core.find_keyed_child("parent", PLUGIN_ID, "race-node")}
        assert len(core.store.descendant_sessions("parent")) == 1
        child = next(iter(results.values()))
        joined = core.join_child("parent", child, timeout_seconds=30)
        assert (joined.status, joined.output) == ("completed", "one winner")
        assert len(server.requests) == 1, "one initial run, not one per contender"


def test_parallel_child_sessions_share_finite_captured_authorization_across_restart(tmp_path: Path) -> None:
    source = tmp_path / "parallel_plugin"
    source.mkdir()
    (source / "__init__.py").write_text(
        "from voidcode.sdk.plugin import Plugin\n"
        "from voidcode.sdk.tools import ToolDefinition, ToolEffect, ToolSuccess, TextOutput\n"
        "class Spawn:\n"
        "    definition = ToolDefinition('spawn_parallel', 'Start two children', effects=frozenset({ToolEffect.SPAWN, ToolEffect.SESSION}))\n"
        "    def invoke(self, call, *, context, services):\n"
        "        ids = [services.spawn_child('parallel-budget-child-' + name, max_steps=None) for name in ('A', 'B')]\n"
        "        for child in ids:\n"
        "            outcome = services.join_child(child, timeout_seconds=30)\n"
        "            if outcome.status != 'completed':\n"
        "                raise RuntimeError(str(outcome.error))\n"
        "        return ToolSuccess(call.tool_name, TextOutput('both independent children completed'))\n"
        "def create_plugin(config):\n"
        "    return Plugin(tools=(Spawn(),))\n",
        encoding="utf-8",
    )
    entered = {name: threading.Event() for name in ("A", "B")}
    release = threading.Event()
    with _server([]) as server:

        def reply(payload, _index):
            text = json.dumps(payload["messages"])
            for name in ("A", "B"):
                if f"parallel-budget-child-{name}" in text:
                    entered[name].set()
                    assert release.wait(timeout=30)
                    return _turn(output=f"child {name} complete")
            return openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "parallel-spawn",
                            "type": "function",
                            "function": {"name": "spawn_parallel", "arguments": "{}"},
                        }
                    ],
                },
                finish="tool_calls",
            )

        server.before_reply = reply
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        plugins = {name: {"enabled": name == "providers"} for name in BUNDLED_PLUGIN_IDS}
        plugins["parallel_fixture"] = {"path": str(source)}
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/exact-model"},
                    "policy": {"approval_mode": "yolo"},
                    "providers": {"endpoint": {"api_key": "parallel-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                    "plugins": plugins,
                }
            ),
            encoding="utf-8",
        )
        core = _open(tmp_path, server)
        outcomes: list[object] = []
        runner = threading.Thread(
            target=lambda: outcomes.append(core.run("start parallel children", session_id="parent", root_action_budget=3)),
            daemon=True,
        )
        try:
            runner.start()
            assert entered["A"].wait(timeout=30), core.list_descendants("parent")
            assert entered["B"].wait(timeout=30), core.list_descendants("parent")
            nodes = core.list_descendants("parent")
            assert len(nodes) == 2 and len({node.session_id for node in nodes}) == 2
            checkpoint = core.store.checkpoint("parent")
            assert checkpoint is not None
            root_id = checkpoint.payload["root_id"]
            assert core.store.root_budget_state(root_id) == (3, 3)
            for node in nodes:
                birth = core._delegation._birth_record(node.session_id)
                assert birth["root_id"] == root_id and birth["root_action_budget"] == 3
        finally:
            release.set()
            runner.join(timeout=30)
        assert not runner.is_alive() and outcomes[0].status == "interrupted"
        assert outcomes[0].budget_exhaustion.dimension == "root_actions"
        assert len(server.requests) == 3
        assert all(core.session(node.session_id).status == "completed" for node in nodes)
        core.close()
        reopened = _open(tmp_path, server)
        resumed = reopened.resume("parent")
        assert resumed.status == "interrupted" and resumed.budget_exhaustion.consumed == 3
        late = reopened.spawn_child(
            "parent",
            "child under exhausted original authorization",
            max_steps=None,
            root_action_id=root_id,
            initial_root_budget=3,
        )
        assert reopened.join_child("parent", late, timeout_seconds=30).status == "interrupted"
        assert reopened.store.root_budget_state(root_id) == (3, 3)
        assert len(server.requests) == 3, "neither completed tool calls nor child dispatches replay on recovery"
