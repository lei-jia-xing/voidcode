"""Committed child capture causes survive every Core mapping to the lease boundary."""

import threading
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from voidcode.core.child_contracts import ChildWaitDelivery
from voidcode.core.delegation import DelegationManager
from voidcode.core.events import EventType
from voidcode.core.plugins import LeaseContext, LeaseIdentity
from voidcode.core.services import RuntimeServices
from voidcode.core.storage.store import SessionRecord, SessionStatus, StoreConflict, StoredEvent
from voidcode.sdk.services import ChildOutcome


def _manager() -> DelegationManager:
    # _child_outcome reads only the committed fact payload, never the core.
    return DelegationManager(cast(Any, None))


def _lease() -> LeaseContext:
    return LeaseContext(LeaseIdentity("token", "owner", "parent", Path("/workspace"), "run", "rev", "invoke", False))


def test_child_outcome_maps_committed_capture_causes() -> None:
    outcome = _manager()._child_outcome(
        "child-1",
        {
            "child_session_id": "child-1",
            "status": "completed",
            "run_id": "run-1",
            "output": None,
            "error": None,
            "stop_cause": "completed",
            "output_bytes": 80000,
            "output_preview": "partial…",
            "output_error": "RuntimeError: boom-output",
            "history": {"sha256": "a" * 64, "bytes": 10},
        },
    )
    assert outcome.output is None and outcome.output_sha256 is None
    assert outcome.output_preview == "partial…"
    assert outcome.output_error == "RuntimeError: boom-output"
    assert outcome.history_error is None and outcome.history_sha256 == "a" * 64


def test_child_outcome_maps_history_capture_failure_without_history_refs() -> None:
    outcome = _manager()._child_outcome(
        "child-1",
        {
            "child_session_id": "child-1",
            "status": "completed",
            "run_id": "run-1",
            "output": "done",
            "error": None,
            "stop_cause": "completed",
            "output_bytes": 4,
            "history_error": "RuntimeError: boom-history",
        },
    )
    assert outcome.output == "done" and outcome.output_error is None
    assert outcome.history_sha256 is None and outcome.history_bytes is None
    assert outcome.history_error == "RuntimeError: boom-history"


def test_child_outcome_treats_missing_causes_as_unknown() -> None:
    outcome = _manager()._child_outcome(
        "child-1",
        {
            "child_session_id": "child-1",
            "status": "completed",
            "run_id": "run-0",
            "output": "done",
            "error": None,
            "stop_cause": "completed",
            "output_bytes": 4,
            "history": {"sha256": "b" * 64, "bytes": 12},
        },
    )
    assert outcome.output_error is None and outcome.history_error is None
    assert outcome.output_preview is None and outcome.output == "done"


def test_as_payload_preserves_preview_and_causes_without_artifact() -> None:
    wire = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        run_id="run-1",
        output_preview="partial…",
        output_error="RuntimeError: boom-output",
        history_error="RuntimeError: boom-history",
    ).as_payload()
    assert wire["output_preview"] == "partial…"
    assert wire["output_error"] == "RuntimeError: boom-output"
    assert wire["history_error"] == "RuntimeError: boom-history"
    assert "output_sha256" not in wire and "output_bytes" not in wire


def test_join_child_parser_preserves_capture_causes() -> None:
    wire = ChildOutcome(
        child_session_id="child-1",
        status="completed",
        run_id="run-1",
        output_preview="partial…",
        output_error="RuntimeError: boom-output",
        history_error="RuntimeError: boom-history",
    )
    runtime = SimpleNamespace(closed=False, join_child_callback=lambda _parent, _child, _timeout: wire)
    outcome = RuntimeServices(cast(Any, runtime), _lease()).join_child("child-1", timeout_seconds=1)
    assert outcome.run_id == "run-1" and outcome.output_sha256 is None
    assert outcome.output_preview == "partial…"
    assert outcome.output_error == "RuntimeError: boom-output"
    assert outcome.history_error == "RuntimeError: boom-history"


def test_wait_children_parser_keeps_exact_run_causes() -> None:
    first = ChildWaitDelivery(
        outcome=ChildOutcome(
            child_session_id="child-1",
            status="completed",
            run_id="run-1",
            output_preview="partial…",
            output_error="RuntimeError: boom-output",
        ),
        source_session_id="parent",
        event_sequence=9,
        notice_id=None,
    )
    second = ChildWaitDelivery(
        outcome=ChildOutcome(
            child_session_id="child-1",
            status="completed",
            run_id="run-2",
            output="followup-ok",
        ),
        source_session_id="parent",
        event_sequence=12,
        notice_id=None,
    )
    runtime = SimpleNamespace(
        closed=False,
        lease_lock=threading.Lock(),
        require_store=lambda: SimpleNamespace(session=lambda _child: SimpleNamespace(parent_session_id="parent")),
        admission_guard=lambda _lease: lambda: False,
        wait_children_callback=lambda _owner, _parent, _children, _guard: (first, second),
    )
    lease = _lease()
    outcomes = RuntimeServices(cast(Any, runtime), lease).wait_children(("child-1",))
    assert [outcome.run_id for outcome in outcomes] == ["run-1", "run-2"]
    assert outcomes[0].output_error == "RuntimeError: boom-output"
    assert outcomes[0].output_preview == "partial…"
    assert outcomes[1].output == "followup-ok" and outcomes[1].output_error is None
    assert len(lease.staging.observer_receipts) == 2


def _stored_event(event_type: str, payload: dict[str, object], *, sequence: int = 1, run_id: str = "run-1") -> StoredEvent:
    return StoredEvent(
        session_id="parent",
        sequence=sequence,
        run_id=run_id,
        event_type=event_type,
        source="core",
        actor="plugin",
        revision_id="rev-1",
        payload=payload,
    )


def _fact_manager(*events: StoredEvent) -> DelegationManager:
    store = SimpleNamespace(events_by_types=lambda *_args: events)
    return DelegationManager(cast(Any, SimpleNamespace(store=store)))


def test_child_fact_requires_an_exact_run_identity() -> None:
    """A fact without its run id must never match every requested run."""
    manager = _fact_manager(_stored_event(EventType.CHILD_FINISHED, {"child_session_id": "child-1", "status": "completed"}))
    with pytest.raises(StoreConflict, match="exact run identity"):
        manager._child_fact("parent", "child-1", "run-1")


def test_child_fact_does_not_match_a_different_run() -> None:
    manager = _fact_manager(_stored_event(EventType.CHILD_FINISHED, {"child_session_id": "child-1", "run_id": "run-0", "status": "completed"}))
    assert manager._child_fact("parent", "child-1", "run-1") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"child_session_id": "child-1", "run_id": "run-1"},
        {"child_session_id": "child-1", "run_id": "run-1", "status": "mystery"},
    ],
)
def test_child_outcome_rejects_missing_or_unknown_status(payload: dict[str, object]) -> None:
    with pytest.raises(StoreConflict, match="status"):
        _manager()._child_outcome("child-1", payload)


@pytest.mark.parametrize("field", ("child_session_id", "run_id"))
def test_child_outcome_rejects_missing_required_identity(field: str) -> None:
    payload: dict[str, object] = {"child_session_id": "child-1", "run_id": "run-1", "status": "completed"}
    del payload[field]
    with pytest.raises(StoreConflict, match=field):
        _manager()._child_outcome("child-1", payload)


def test_child_outcome_rejects_misattributed_child() -> None:
    with pytest.raises(StoreConflict, match="names child"):
        _manager()._child_outcome("child-1", {"child_session_id": "child-2", "run_id": "run-1", "status": "completed"})


@pytest.mark.parametrize(("field", "value"), (("output", 1), ("error", ["x"]), ("stop_cause", 5), ("output_error", 7)))
def test_child_outcome_rejects_coerced_optional_strings(field: str, value: object) -> None:
    payload: dict[str, object] = {"child_session_id": "child-1", "run_id": "run-1", "status": "completed", field: value}
    with pytest.raises(StoreConflict, match=field):
        _manager()._child_outcome("child-1", payload)


def test_child_outcome_keeps_absent_optional_causes_as_unknown() -> None:
    outcome = _manager()._child_outcome("child-1", {"child_session_id": "child-1", "run_id": "run-1", "status": "failed"})
    assert outcome.output is None and outcome.error is None and outcome.stop_cause is None


def test_child_run_facts_records_provider_result_decode_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SessionRecord(
        session_id="child-1",
        workspace="/workspace",
        status=SessionStatus.COMPLETED,
        active_revision_id="rev-1",
        run_id="run-1",
        turn=1,
        last_event_sequence=2,
        parent_session_id="parent",
    )
    events = [
        _stored_event(EventType.MODEL_TURN, {"result": {"output": "lost output"}}, sequence=1),
        _stored_event(EventType.RUN_COMPLETED, {}, sequence=2),
    ]

    def fail_decode(_payload: Mapping[str, object]) -> object:
        raise ValueError("decode boom")

    monkeypatch.setattr("voidcode.core.delegation._outcomes.decode_provider_result", fail_decode)
    facts = _fact_manager()._child_run_facts(session, "run-1", events)
    assert facts is not None
    assert facts.output is None
    assert facts.output_error == "ValueError: decode boom"


def test_child_run_facts_records_a_malformed_provider_result() -> None:
    session = SessionRecord(
        session_id="child-1",
        workspace="/workspace",
        status=SessionStatus.COMPLETED,
        active_revision_id="rev-1",
        run_id="run-1",
        turn=1,
        last_event_sequence=2,
        parent_session_id="parent",
    )
    events = [
        _stored_event(EventType.MODEL_TURN, {"result": None}, sequence=1),
        _stored_event(EventType.RUN_COMPLETED, {}, sequence=2),
    ]
    facts = _fact_manager()._child_run_facts(session, "run-1", events)
    assert facts is not None
    assert facts.output is None
    assert facts.output_error is not None and "is not an object" in facts.output_error
