"""Wakeup claim admission, staged atomicity, and startup recovery over real SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.events import EventType
from voidcode.core.storage.store import (
    CheckpointKind,
    EventDraft,
    SequenceConflict,
    SessionStatus,
    SqliteStore,
    StagedWakeup,
    StagedWakeupCancel,
)

WS = "/ws"
ROOT = "root-1"


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "wakeup-claims.sqlite3")
    revision = store.put_revision({"plugins": []})
    store.record_workspace_revision(WS, revision)
    return store


def _session(store: SqliteStore, session_id: str) -> str:
    revision = store.workspace_current(WS)
    assert revision is not None
    store.create_session(session_id, WS, revision)
    return revision


def _checkpoint(store: SqliteStore, session_id: str, *, status: SessionStatus, kind: CheckpointKind, payload: dict[str, object]) -> None:
    revision = store.workspace_current(WS)
    assert revision is not None
    session = store.session(session_id)
    store.commit_staged(
        session_id,
        expected_sequence=session.last_event_sequence,
        expected_run_id=session.run_id,
        run_id=session.run_id or "run-setup",
        revision_id=revision,
        events=(),
        status=status,
        checkpoint_kind=kind,
        checkpoint_payload=payload,
    )


def _schedule(store: SqliteStore, wakeup_id: str, session_id: str, *, due_at: int = 1) -> None:
    store.schedule_wakeup(wakeup_id, "owner", WS, session_id, {"text": wakeup_id}, due_at, root_action_id=ROOT, root_action_budget=None)


def _wakeup_row(store: SqliteStore, wakeup_id: str) -> tuple[str, str | None]:
    with store._lock:
        row = store._connection.execute("SELECT status, claim_run_id FROM wakeups WHERE wakeup_id = ?", (wakeup_id,)).fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


def _claim(store: SqliteStore):
    revision = store.workspace_current(WS)
    assert revision is not None
    return store.claim_due_wakeups(WS, 10, revision_id=revision, eligible_owners=("owner",))


def test_one_busy_head_never_starves_another_due_session(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _session(store, "busy")
        _session(store, "free")
        _checkpoint(store, "busy", status=SessionStatus.RUNNING, kind=CheckpointKind.READY, payload={"batch_sequence": 4, "root_id": ROOT})
        busy_before = store.checkpoint("busy")
        assert busy_before is not None
        _schedule(store, "wake-busy", "busy", due_at=1)
        _schedule(store, "wake-free", "free", due_at=2)

        claims, skipped = _claim(store)

        assert [wakeup.wakeup_id for wakeup, _session in claims] == ["wake-free"]
        assert [(item.wakeup_id, item.reason) for item in skipped] == [("wake-busy", "session_running")]
        assert _wakeup_row(store, "wake-busy")[0] == "pending"
        assert _wakeup_row(store, "wake-free")[0] == "claimed"
        busy_after = store.checkpoint("busy")
        assert busy_after is not None
        assert busy_after.kind is CheckpointKind.READY
        assert busy_after.payload == busy_before.payload
        fired = [event for event in store.events("free") if event.event_type == EventType.WAKEUP_FIRED]
        assert len(fired) == 1
        assert fired[0].run_id == claims[0][0].claim_run_id


@pytest.mark.parametrize(
    ("status", "kind", "reason"),
    [
        (SessionStatus.INTERRUPTED, CheckpointKind.INTERRUPTED, "checkpoint_interrupted"),
        (SessionStatus.WAITING, CheckpointKind.APPROVAL_WAIT, "session_parked"),
        (SessionStatus.COMPLETED, CheckpointKind.READY, "checkpoint_ready"),
    ],
)
def test_claim_guard_preserves_a_resumable_or_parked_checkpoint(tmp_path: Path, status: SessionStatus, kind: CheckpointKind, reason: str) -> None:
    with _store(tmp_path) as store:
        _session(store, "sess")
        payload = {"batch_sequence": 7, "next_call": 2, "root_id": ROOT, "initial_root_budget": None, "logical_turn": "lt"}
        _checkpoint(store, "sess", status=status, kind=kind, payload=payload)
        before = store.checkpoint("sess")
        assert before is not None
        _schedule(store, "wake", "sess")

        claims, skipped = _claim(store)

        assert claims == []
        assert [(item.wakeup_id, item.reason) for item in skipped] == [("wake", reason)]
        assert _wakeup_row(store, "wake")[0] == "pending"
        after = store.checkpoint("sess")
        assert after is not None
        assert after.kind is kind
        assert after.run_id == before.run_id
        assert after.payload == before.payload
        assert not [event for event in store.events("sess") if event.event_type == EventType.WAKEUP_FIRED]


def test_cancelled_terminal_interruption_admits_a_fresh_wakeup(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _session(store, "sess")
        _checkpoint(store, "sess", status=SessionStatus.INTERRUPTED, kind=CheckpointKind.INTERRUPTED, payload={"root_id": ROOT})
        stopped = store.session("sess")
        # The same durable run ends deliberately: interrupted session, terminal checkpoint.
        store.commit_staged(
            "sess",
            expected_sequence=stopped.last_event_sequence,
            expected_run_id=stopped.run_id,
            run_id=stopped.run_id or "run-cancelled",
            revision_id=store.workspace_current(WS) or "",
            events=(
                EventDraft(
                    EventType.RUN_INTERRUPTED,
                    "core",
                    "core",
                    {"reason": "user stop", "stop_cause": "user_stop", "logical_turn": "lt"},
                ),
            ),
            status=SessionStatus.INTERRUPTED,
            checkpoint_kind=CheckpointKind.TERMINAL,
            checkpoint_payload={"root_id": ROOT},
        )
        _schedule(store, "wake", "sess")

        claims, skipped = _claim(store)

        assert [wakeup.wakeup_id for wakeup, _session in claims] == ["wake"]
        assert skipped == []
        checkpoint = store.checkpoint("sess")
        assert checkpoint is not None
        assert checkpoint.kind is CheckpointKind.READY
        assert store.session("sess").status is SessionStatus.RUNNING


def test_staged_wakeup_schedule_and_cancel_are_one_transaction(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        revision = _session(store, "sess")
        _schedule(store, "old", "sess", due_at=5)
        assert _wakeup_row(store, "old")[0] == "pending"
        session = store.session("sess")

        with pytest.raises(SequenceConflict):
            store.commit_staged(
                "sess",
                expected_sequence=session.last_event_sequence + 1,
                expected_run_id=session.run_id,
                run_id="run-1",
                revision_id=revision,
                events=(),
                status=SessionStatus.RUNNING,
                checkpoint_kind=CheckpointKind.READY,
                checkpoint_payload={"root_id": ROOT},
                staged_wakeups=(StagedWakeup(wakeup_id="new", owner="owner", session_id="sess", payload={"text": "new"}, due_at=0),),
                staged_wakeup_cancels=(StagedWakeupCancel(wakeup_id="old", owner="owner"),),
                root_action_id=ROOT,
            )
        assert _wakeup_row(store, "old")[0] == "pending"
        with store._lock:
            assert store._connection.execute("SELECT 1 FROM wakeups WHERE wakeup_id = 'new'").fetchone() is None

        store.commit_staged(
            "sess",
            expected_sequence=session.last_event_sequence,
            expected_run_id=session.run_id,
            run_id="run-1",
            revision_id=revision,
            events=(),
            status=SessionStatus.RUNNING,
            checkpoint_kind=CheckpointKind.READY,
            checkpoint_payload={"root_id": ROOT},
            staged_wakeups=(StagedWakeup(wakeup_id="new", owner="owner", session_id="sess", payload={"text": "new"}, due_at=0),),
            staged_wakeup_cancels=(StagedWakeupCancel(wakeup_id="old", owner="owner"),),
            root_action_id=ROOT,
        )
        assert _wakeup_row(store, "old")[0] == "cancelled"
        assert _wakeup_row(store, "new")[0] == "pending"
        event_count = len(store.events("sess"))

        # A keyed retry of the same staged identity is a no-op, not a conflict.
        store.commit_staged(
            "sess",
            expected_sequence=store.session("sess").last_event_sequence,
            expected_run_id="run-1",
            run_id="run-1",
            revision_id=revision,
            events=(),
            status=SessionStatus.RUNNING,
            checkpoint_kind=CheckpointKind.READY,
            checkpoint_payload={"root_id": ROOT},
            staged_wakeups=(StagedWakeup(wakeup_id="new", owner="owner", session_id="sess", payload={"text": "new"}, due_at=0),),
            root_action_id=ROOT,
        )
        assert _wakeup_row(store, "new")[0] == "pending"
        assert len(store.events("sess")) == event_count


def test_recovery_settles_a_dead_claimed_run_exactly_once(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        revision = _session(store, "sess")
        _schedule(store, "wake", "sess")
        claims, _skipped = _claim(store)
        assert len(claims) == 1
        before_checkpoint = store.checkpoint("sess")
        assert before_checkpoint is not None
        events_before = len(store.events("sess"))

        recovered = store.recover_claimed_wakeup_runs(WS, revision_id=revision, fact_observers={"run_finished": ("owner",)})
        assert recovered == (("sess", "failed"),)
        session = store.session("sess")
        assert session.status is SessionStatus.INTERRUPTED
        assert _wakeup_row(store, "wake")[0] == "failed"
        checkpoint = store.checkpoint("sess")
        assert checkpoint is not None
        assert checkpoint.kind is CheckpointKind.INTERRUPTED
        assert checkpoint.payload == before_checkpoint.payload
        events = store.events("sess")
        assert len(events) == events_before + 1
        interrupted = events[-1]
        assert interrupted.event_type == EventType.RUN_INTERRUPTED
        assert interrupted.payload["stop_cause"] == "shutdown"
        assert tuple(interrupted.payload["eligible_owners"]) == ("owner",)
        assert interrupted.run_id == claims[0][0].claim_run_id

        assert store.recover_claimed_wakeup_runs(WS, revision_id=revision, fact_observers={"run_finished": ("owner",)}) == ()
        assert len(store.events("sess")) == events_before + 1


def test_recovery_keeps_a_parked_claim_and_settles_finished_work_without_replay(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        revision = _session(store, "parked")
        _session(store, "finished")
        _schedule(store, "wake-parked", "parked")
        _schedule(store, "wake-finished", "finished")
        claims, _skipped = _claim(store)
        claims_by_id = {wakeup.wakeup_id: session for wakeup, session in claims}
        assert set(claims_by_id) == {"wake-parked", "wake-finished"}
        parked_claim = claims_by_id["wake-parked"]
        finished_claim = claims_by_id["wake-finished"]
        # Park one claimed run for approval and let the other record a terminal completion.
        store.commit_staged(
            "parked",
            expected_sequence=parked_claim.last_event_sequence,
            expected_run_id=parked_claim.run_id,
            run_id=parked_claim.run_id or "run-parked",
            revision_id=revision,
            events=(EventDraft(EventType.APPROVAL_REQUESTED, "core", "model", {"request_id": "req"}),),
            status=SessionStatus.WAITING,
            checkpoint_kind=CheckpointKind.APPROVAL_WAIT,
            checkpoint_payload={"root_id": ROOT, "approval_sequence": 1},
        )
        store.commit_staged(
            "finished",
            expected_sequence=finished_claim.last_event_sequence,
            expected_run_id=finished_claim.run_id,
            run_id=finished_claim.run_id or "run-finished",
            revision_id=revision,
            events=(EventDraft(EventType.RUN_COMPLETED, "core", "core", {"logical_turn": "lt"}),),
            status=SessionStatus.COMPLETED,
            checkpoint_kind=CheckpointKind.TERMINAL,
            checkpoint_payload={"root_id": ROOT},
        )
        finished_events = len(store.events("finished"))
        finished_checkpoint = store.checkpoint("finished")
        assert finished_checkpoint is not None

        recovered = store.recover_claimed_wakeup_runs(WS, revision_id=revision, fact_observers={"run_finished": ("owner",)})

        assert recovered == ()
        assert store.session("parked").status is SessionStatus.WAITING
        assert _wakeup_row(store, "wake-parked")[0] == "claimed"
        assert _wakeup_row(store, "wake-finished")[0] == "completed"
        assert len(store.events("finished")) == finished_events
        after = store.checkpoint("finished")
        assert after is not None and after.payload == finished_checkpoint.payload
