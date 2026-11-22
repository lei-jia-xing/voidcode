"""Unit tests for continuation scheduler domain (ContinuationScheduler, ContinuationDrainSpec)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from voidcode.core.cancellation import Cancellation
from voidcode.core.engine.continuation_scheduler import (
    ContinuationDrainSpec,
    ContinuationScheduler,
    ContinuationSchedulerHost,
)
from voidcode.core.storage.store import (
    Checkpoint,
    CheckpointKind,
    ContinuationRecord,
    ContinuationStatus,
    SessionRecord,
    SessionStatus,
    StagedCancellation,
)


def _make_session(session_id: str = "sess-cont-1") -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        workspace="/tmp/ws",
        status=SessionStatus.RUNNING,
        active_revision_id="rev-1",
        run_id="run-1",
        turn=1,
        last_event_sequence=1,
    )


def _make_cont_record(
    continuation_id: str = "cont-1",
    root_id: str | None = "root-1",
    session_id: str = "sess-cont-1",
    status: ContinuationStatus = ContinuationStatus.PENDING,
) -> ContinuationRecord:
    return ContinuationRecord(
        continuation_id=continuation_id,
        owner="plugin-1",
        session_id=session_id,
        source="core",
        run_id="run-1",
        revision_id="rev-1",
        idempotency_key=None,
        status=status,
        payload={},
        created_at=1000,
        updated_at=1000,
        root_id=root_id,
    )


def test_continuation_drain_spec_dataclass() -> None:
    spec = ContinuationDrainSpec(session_id="s1", limit=5, max_steps=10, continuation_id="c1", automatic=True)
    assert spec.session_id == "s1"
    assert spec.limit == 5
    assert spec.max_steps == 10
    assert spec.continuation_id == "c1"
    assert spec.automatic is True


def test_continuation_scheduler_drain_empty() -> None:
    host = MagicMock(spec=ContinuationSchedulerHost)
    host.store.claimed_continuations.return_value = ()
    host.store.pending_continuations.return_value = ()
    host.session.return_value = _make_session()

    @contextmanager
    def lock_cm(sid: str):
        yield

    host.session_lock.side_effect = lock_cm

    scheduler = ContinuationScheduler(host)
    results = scheduler.drain("sess-cont-1", limit=1)
    assert results == ()


def test_continuation_scheduler_drain_missing_root_id() -> None:
    host = MagicMock(spec=ContinuationSchedulerHost)
    host.store.claimed_continuations.return_value = ()
    pending_record = _make_cont_record(continuation_id="cont-bad", root_id="")
    host.store.pending_continuations.return_value = (pending_record,)
    host.session.return_value = SessionRecord(
        session_id="sess-cont-1",
        workspace="/tmp/ws",
        status=SessionStatus.IDLE,
        active_revision_id="rev-1",
        run_id="run-1",
        turn=1,
        last_event_sequence=1,
    )

    @contextmanager
    def lock_cm(sid: str):
        yield

    host.session_lock.side_effect = lock_cm

    scheduler = ContinuationScheduler(host)
    results = scheduler.drain("sess-cont-1", limit=1)
    assert len(results) == 1
    assert results[0].status == "failed"
    assert "no captured root action identity" in (results[0].error or "")
    host.store.complete_continuation.assert_called_once_with("cont-bad", status=ContinuationStatus.FAILED)


def test_continuation_scheduler_drain_success() -> None:
    host = MagicMock(spec=ContinuationSchedulerHost)
    host.revision_id = "rev-1"
    host.chain_of.return_value = frozenset()
    host.store.claimed_continuations.return_value = ()
    cont_record = _make_cont_record(continuation_id="cont-1", root_id="root-1")
    sess_record = SessionRecord(
        session_id="sess-cont-1",
        workspace="/tmp/ws",
        status=SessionStatus.IDLE,
        active_revision_id="rev-1",
        run_id="run-1",
        turn=1,
        last_event_sequence=1,
    )
    host.session.return_value = sess_record
    host.store.pending_continuations.return_value = (cont_record,)
    host.store.claim_and_start_continuation.return_value = (cont_record, sess_record)
    host.store.session.return_value = sess_record
    host.store.root_budget_state.return_value = (100, 10)
    host.store.events.return_value = ()

    run_res = MagicMock()
    run_res.status = "completed"
    run_res.run_id = "run-res-1"
    run_res.output = "done"
    run_res.error = None
    run_res.stop_cause = "completed"
    run_res.budget_exhaustion = None
    host.drive.return_value = run_res

    @contextmanager
    def lock_cm(sid: str):
        yield

    host.session_lock.side_effect = lock_cm

    scheduler = ContinuationScheduler(host)
    results = scheduler.drain("sess-cont-1", limit=1)
    assert len(results) == 1
    assert results[0].status == "completed"
    assert results[0].output == "done"
    host.store.complete_continuation.assert_called_once_with("cont-1", status=ContinuationStatus.COMPLETED)


def test_continuation_scheduler_settle_cancellations() -> None:
    host = MagicMock(spec=ContinuationSchedulerHost)
    scheduler = ContinuationScheduler(host)
    scheduler.settle_cancellations("sess-cont-1", ())
    host.store.checkpoint.assert_not_called()

    checkpoint = Checkpoint(
        kind=CheckpointKind.READY,
        revision_id="rev-1",
        run_id="run-1",
        last_event_sequence=1,
        payload={"continuation_id": "cont-1"},
    )
    host.store.checkpoint.return_value = checkpoint
    host.store.continuation_owner.return_value = "owner-1"

    active_abort = Cancellation()
    host.active_abort.return_value = active_abort

    cancel = StagedCancellation(owner="owner-1", continuation_id="cont-1")
    scheduler.settle_cancellations("sess-cont-1", (cancel,), reason="user cancelled")
    assert active_abort.cancelled is True
    assert active_abort.reason == "user cancelled"
    assert active_abort.stop_cause == "owner_cancel"
