"""Unit tests for atomic commit and staging pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from voidcode.core.engine.commit_pipeline import (
    CheckpointBinding,
    CommitHost,
    CommitPipeline,
    RootBudgetGrant,
    StagedTurnBatch,
)
from voidcode.core.events import EventType
from voidcode.core.execution import _Cursor, _Execution
from voidcode.core.storage.store import (
    Checkpoint,
    CheckpointKind,
    EventDraft,
    SequenceConflict,
    SessionRecord,
    SessionStatus,
    StagedContinuationDraft,
    StoreConflict,
    StoredEvent,
)


def _make_session(
    session_id: str = "sess-1",
    last_event_sequence: int = 0,
    run_id: str | None = "run-1",
    status: SessionStatus = SessionStatus.RUNNING,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        workspace="/tmp/ws",
        status=status,
        active_revision_id="rev-1",
        run_id=run_id,
        turn=1,
        last_event_sequence=last_event_sequence,
    )


def _make_stored_event(
    session_id: str = "sess-1",
    sequence: int = 1,
    event_type: str = EventType.TOOL_INTENT,
    payload: dict[str, object] | None = None,
) -> StoredEvent:
    return StoredEvent(
        session_id=session_id,
        sequence=sequence,
        run_id="run-1",
        event_type=event_type,
        source="core",
        actor="model",
        revision_id="rev-1",
        payload=payload or {},
    )


def test_checkpoint_binding_and_root_budget_grant_dataclasses() -> None:
    binding = CheckpointBinding(field_name="ask_seq", event_type=EventType.ASK_REQUESTED)
    assert binding.field_name == "ask_seq"
    assert binding.event_type == EventType.ASK_REQUESTED

    grant = RootBudgetGrant(owner="plugin_a", amount=100, source_run_id="run-0")
    assert grant.owner == "plugin_a"
    assert grant.amount == 100
    assert grant.source_run_id == "run-0"
    assert grant.as_tuple() == ("plugin_a", 100, "run-0")


def test_assert_execution_prefix_matches_and_raises() -> None:
    sess = _make_session(last_event_sequence=2)
    ev1 = _make_stored_event(sequence=1)
    ev2 = _make_stored_event(sequence=2)
    cursor = _Cursor()
    exec_valid = _Execution(sess, cursor, [ev1, ev2], "run-1")

    assert CommitPipeline.assert_execution_prefix(exec_valid) == 2

    # Tip mismatch with event count
    sess_bad = _make_session(last_event_sequence=3)
    exec_bad_tip = _Execution(sess_bad, cursor, [ev1, ev2], "run-1")
    with pytest.raises(ValueError, match="differs from in-memory prefix"):
        CommitPipeline.assert_execution_prefix(exec_bad_tip)

    # Event sequence mismatch with event count
    ev_bad = _make_stored_event(sequence=99)
    exec_bad_seq = _Execution(sess, cursor, [ev1, ev_bad], "run-1")
    with pytest.raises(ValueError, match="expected 2"):
        CommitPipeline.assert_execution_prefix(exec_bad_seq)


def test_reconcile_execution_events_branches() -> None:
    host = MagicMock(spec=CommitHost)
    pipeline = CommitPipeline(host)

    sess = _make_session(last_event_sequence=1)
    ev1 = _make_stored_event(sequence=1)
    cursor = _Cursor()
    execution = _Execution(sess, cursor, [ev1], "run-1")

    # 1. Backwards tip raises
    with pytest.raises(ValueError, match="cannot reconcile backwards"):
        pipeline.reconcile_execution_events(execution, upto=0)

    # 2. Session snapshot mismatch raises
    sess_target = _make_session(last_event_sequence=5)
    with pytest.raises(ValueError, match="does not match its session snapshot"):
        pipeline.reconcile_execution_events(execution, upto=4, session=sess_target)

    # 3. Incomplete prefix raises
    host.store.events.return_value = ()
    with pytest.raises(ValueError, match="incomplete"):
        pipeline.reconcile_execution_events(execution, upto=2)

    # 4. Non-contiguous sequence raises
    ev_gap = _make_stored_event(sequence=5)
    host.store.events.return_value = (ev_gap,)
    with pytest.raises(ValueError, match="not contiguous"):
        pipeline.reconcile_execution_events(execution, upto=2)

    # 5. Intervening event violates parked prefix
    ev2_violating = _make_stored_event(sequence=2, event_type="unregistered.unknown.event")
    host.store.events.return_value = (ev2_violating,)
    with pytest.raises(ValueError, match="violates the parked checkpoint prefix"):
        pipeline.reconcile_execution_events(execution, upto=2, parked=True)

    # 6. Success append
    ev2 = _make_stored_event(sequence=2, event_type=EventType.MODEL_QUERY)
    host.store.events.return_value = (ev2,)
    new_sess = _make_session(last_event_sequence=2)
    pipeline.reconcile_execution_events(execution, upto=2, session=new_sess, parked=True)
    assert len(execution.events) == 2
    assert execution.session == new_sess


def test_bind_checkpoint_refs() -> None:
    cursor = _Cursor()
    ev1 = _make_stored_event(sequence=10, event_type=EventType.TOOL_INTENT)
    ev2 = _make_stored_event(sequence=11, event_type=EventType.ASK_REQUESTED)

    bindings = (
        CheckpointBinding("intent_sequence", EventType.TOOL_INTENT),
        ("ask_sequence", EventType.ASK_REQUESTED),
    )
    CommitPipeline.bind_checkpoint_refs(cursor, (ev1, ev2), bindings)
    assert cursor.intent_sequence == 10
    assert cursor.ask_sequence == 11

    # Missing event raises
    with pytest.raises(ValueError, match="has no committed"):
        CommitPipeline.bind_checkpoint_refs(cursor, (ev1,), bindings)


def test_stamp_fact_owners() -> None:
    host = MagicMock(spec=CommitHost)
    host.observer_owners = {"invocation_finished": ("plugin_tools",), "usage": ("plugin_usage",)}
    pipeline = CommitPipeline(host)

    ev_tool = EventDraft(
        EventType.TOOL_COMPLETED,
        "plugin_tools",
        "tool",
        {"call_id": "c1"},
    )
    ev_intent = EventDraft(
        EventType.PROVIDER_INTENT,
        "core",
        "model",
        {"tokens": 10},
    )
    ev_already_stamped = EventDraft(
        EventType.TOOL_COMPLETED,
        "plugin_tools",
        "tool",
        {"call_id": "c2", "eligible_owners": ["pre_stamped"]},
    )
    ev_non_fact = EventDraft(
        EventType.TOOL_PROGRESS,
        "core",
        "system",
        {},
    )

    stamped = pipeline.stamp_fact_owners((ev_tool, ev_intent, ev_already_stamped, ev_non_fact))
    assert len(stamped) == 4
    assert tuple(stamped[0].payload["eligible_owners"]) == ("plugin_tools",)
    assert tuple(stamped[1].payload["eligible_owners"]) == ("plugin_usage",)
    assert tuple(stamped[2].payload["eligible_owners"]) == ("pre_stamped",)
    assert "eligible_owners" not in stamped[3].payload


def test_deliver_committed_facts() -> None:
    host = MagicMock(spec=CommitHost)
    pipeline = CommitPipeline(host)

    # 1. Skip if session is already delivering
    host.delivering_session.return_value = True
    pipeline.deliver_committed_facts("s1", after_sequence=0, upto_sequence=2, run_id="r1")
    host.deliver_facts.assert_not_called()

    # 2. Normal delivery
    host.delivering_session.return_value = False
    sess = _make_session("s1", last_event_sequence=2)
    host.store.session.return_value = sess
    host.store.checkpoint.return_value = Checkpoint(
        kind=CheckpointKind.READY,
        revision_id="rev-1",
        run_id="r1",
        last_event_sequence=2,
        payload={"root_id": "root-1"},
    )
    ev1 = _make_stored_event("s1", sequence=1)
    ev2 = _make_stored_event("s1", sequence=2)
    host.store.events.return_value = (ev1, ev2)

    pipeline.deliver_committed_facts("s1", after_sequence=1, upto_sequence=2, run_id="r1")
    host.deliver_facts.assert_called_once()
    args, kwargs = host.deliver_facts.call_args
    assert len(args[1]) == 1
    assert args[1][0] == ev2
    assert kwargs["tolerate_conflicts"] is True


def test_commit_staged_batch_and_retry() -> None:
    host = MagicMock(spec=CommitHost)
    host.revision_id = "rev-1"
    host.observer_owners = {}
    pipeline = CommitPipeline(host)

    sess = _make_session("s1", last_event_sequence=1, run_id="run-1")
    ev1 = _make_stored_event("s1", sequence=1)
    cursor = _Cursor(root_id="root-1", initial_root_budget=10)
    execution = _Execution(sess, cursor, [ev1], "run-1")

    # 1. Enforce pin failure
    batch_pinned_fail = StagedTurnBatch(
        status=SessionStatus.RUNNING,
        kind=CheckpointKind.READY,
        pinned_run_id="other-run",
        enforce_pin=True,
    )
    with pytest.raises(StoreConflict, match="does not match bound run"):
        pipeline.commit_staged_batch(execution, batch_pinned_fail)

    # 2. SequenceConflict retries and succeeds
    ev_draft = EventDraft(EventType.TOOL_PROGRESS, "core", "tool", {})
    ev2 = _make_stored_event("s1", sequence=2, event_type=EventType.TOOL_PROGRESS)
    sess_committed = _make_session("s1", last_event_sequence=2, run_id="run-1")

    host.store.session.return_value = sess
    host.store.commit_staged.side_effect = [
        SequenceConflict("tip moved"),
        sess_committed,
    ]
    host.store.events.return_value = (ev2,)

    batch = StagedTurnBatch(
        status=SessionStatus.RUNNING,
        kind=CheckpointKind.READY,
        events=(ev_draft,),
        reserve_root=True,
        root_budget_grant=RootBudgetGrant("plugin_b", 50, "run-1"),
        staged_continuations=(StagedContinuationDraft(continuation_id="c-1", owner="core", source="turn", payload={}),),
        checkpoint_bindings=(CheckpointBinding("intent_sequence", EventType.TOOL_PROGRESS),),
    )

    new_events = pipeline.commit_staged_batch(execution, batch)
    assert len(new_events) == 1
    assert new_events[0] == ev2
    assert host.store.commit_staged.call_count == 2
    assert host.notify_commit.call_count == 1
    assert cursor.intent_sequence == 2


def test_commit_turn_pipeline_delivers_facts_and_notifies() -> None:
    host = MagicMock(spec=CommitHost)
    host.revision_id = "rev-1"
    host.observer_owners = {}
    pipeline = CommitPipeline(host)

    sess = _make_session("s1", last_event_sequence=0, run_id="run-1")
    cursor = _Cursor()
    execution = _Execution(sess, cursor, [], "run-1")

    ev_draft = EventDraft(EventType.TOOL_INTENT, "core", "tool", {})
    ev1 = _make_stored_event("s1", sequence=1, event_type=EventType.TOOL_INTENT)
    sess_committed = _make_session("s1", last_event_sequence=1, run_id="run-1")

    host.store.session.return_value = sess
    host.store.commit_staged.return_value = sess_committed
    host.store.events.return_value = (ev1,)

    committed = pipeline.commit(
        execution,
        SessionStatus.RUNNING,
        CheckpointKind.READY,
        ev_draft,
    )
    assert committed == (ev1,)
    host.notify_tool_waiters.assert_called_once()
    host.deliver_facts.assert_called_once()


def test_commit_public_staged_and_fact() -> None:
    host = MagicMock(spec=CommitHost)
    host.revision_id = "rev-1"
    host.observer_owners = {}
    host.delivering_session.return_value = False
    pipeline = CommitPipeline(host)

    sess = _make_session("s1", last_event_sequence=0, run_id="run-1")
    sess_after = _make_session("s1", last_event_sequence=1, run_id="run-1")
    host.store.session.return_value = sess
    host.store.checkpoint.return_value = None
    host.store.commit_staged.return_value = sess_after
    host.store.events.return_value = ()

    ev_draft = EventDraft(EventType.CHILD_SPAWNED, "core", "parent", {})
    result = pipeline.commit_public_fact("s1", ev_draft)
    assert result == sess_after
    host.notify_commit.assert_called_once()
