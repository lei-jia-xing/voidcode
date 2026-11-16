"""Committed child capture causes survive every Core mapping to the lease boundary."""

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from voidcode.core.delegation import DelegationManager
from voidcode.core.plugins import LeaseContext
from voidcode.core.services import RuntimeServices
from voidcode.sdk.services import ChildOutcome


def _manager() -> DelegationManager:
    # _child_outcome reads only the committed fact payload, never the core.
    return DelegationManager(cast(Any, None))


def _lease() -> LeaseContext:
    return LeaseContext("token", "owner", "parent", Path("/workspace"), "run", "rev", "invoke", False)


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
    ).as_payload()
    runtime = SimpleNamespace(closed=False, join_child_callback=lambda _parent, _child, _timeout: wire)
    outcome = RuntimeServices(cast(Any, runtime), _lease()).join_child("child-1", timeout_seconds=1)
    assert outcome.run_id == "run-1" and outcome.output_sha256 is None
    assert outcome.output_preview == "partial…"
    assert outcome.output_error == "RuntimeError: boom-output"
    assert outcome.history_error == "RuntimeError: boom-history"


def test_wait_children_parser_keeps_exact_run_causes() -> None:
    first: dict[str, Any] = {
        "outcome": ChildOutcome(
            child_session_id="child-1",
            status="completed",
            run_id="run-1",
            output_preview="partial…",
            output_error="RuntimeError: boom-output",
        ).as_payload(),
        "source_session_id": "parent",
        "event_sequence": 9,
        "notice_id": None,
    }
    second: dict[str, Any] = {
        "outcome": ChildOutcome(
            child_session_id="child-1",
            status="completed",
            run_id="run-2",
            output="followup-ok",
        ).as_payload(),
        "source_session_id": "parent",
        "event_sequence": 12,
        "notice_id": None,
    }
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
    assert len(lease.staged_observer_receipts) == 2
