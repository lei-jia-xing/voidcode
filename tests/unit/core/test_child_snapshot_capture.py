"""The snapshot's exact terminal-run diagnostics stay distinct from eligible output."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest

from voidcode.core.delegation import DelegationManager
from voidcode.core.events import EventType
from voidcode.core.storage.store import StoreConflict, StoredEvent
from voidcode.sdk.services import ChildSessionSnapshot, DescendantNode

_CHILD = "child-1"
_PARENT = "parent-1"
_DIGEST_A = "a" * 64


def _event(payload: Mapping[str, object], sequence: int) -> StoredEvent:
    return StoredEvent(
        session_id=_PARENT,
        sequence=sequence,
        run_id="parent-run",
        event_type=EventType.CHILD_FINISHED,
        source="core",
        actor="plugin",
        revision_id="rev-1",
        payload=dict(payload),
    )


def _fact(run_id: str, status: str, **extra: object) -> dict[str, object]:
    return {"child_session_id": _CHILD, "run_id": run_id, "status": status, "notify_on_completion": False, **extra}


def _node() -> DescendantNode:
    return DescendantNode(
        session_id=_CHILD,
        parent_session_id=_PARENT,
        depth=1,
        status="running",
        run_id="run-current",
        last_event_sequence=99,
    )


def _snapshot(monkeypatch: pytest.MonkeyPatch, *events: StoredEvent) -> ChildSessionSnapshot:
    facts = list(events)
    store = SimpleNamespace(events_by_types=lambda *_args: facts)
    manager = DelegationManager(cast(Any, SimpleNamespace(ensure_open=lambda: None, store=store)))
    pinned = _node()
    monkeypatch.setattr(manager, "list_descendants", lambda _caller: (pinned,))
    snapshot = manager.read_child_snapshot(_PARENT, _CHILD)
    assert snapshot is not None
    return snapshot


def test_exact_terminal_run_is_not_the_latest_eligible_output(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(
        monkeypatch,
        _event(_fact("run-A", "completed", output="accepted output", output_sha256=_DIGEST_A, output_bytes=15), sequence=2),
        _event(_fact("run-C", "failed", error="provider failed", stop_cause="error"), sequence=6),
    )

    assert snapshot.latest_outcome is not None and snapshot.latest_outcome.run_id == "run-A"
    assert snapshot.latest_outcome.output == "accepted output"
    run = snapshot.terminal_run
    assert run is not None
    assert (run.run_id, run.status, run.terminal_sequence) == ("run-C", "failed", 6)
    assert run.capture == "not_completed" and not run.eligible
    assert run.stop_cause == "error"
    assert run.output_sha256 is None and run.output_preview is None


def test_preview_only_terminal_run_keeps_its_own_preview_and_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(
        monkeypatch,
        _event(_fact("run-A", "completed", output="accepted output", output_sha256=_DIGEST_A, output_bytes=15), sequence=2),
        _event(
            _fact(
                "run-B",
                "completed",
                output_preview="partial B",
                output_bytes=90_000,
                output_error="ArtifactStoreError: quota exceeded",
            ),
            sequence=5,
        ),
    )

    assert snapshot.latest_outcome is not None and snapshot.latest_outcome.run_id == "run-A"
    run = snapshot.terminal_run
    assert run is not None
    assert (run.run_id, run.status, run.capture, run.terminal_sequence) == ("run-B", "completed", "preview_only", 5)
    assert run.eligible is False
    assert run.output_preview == "partial B"
    assert run.output_bytes == 90_000
    assert run.output_error == "ArtifactStoreError: quota exceeded"
    # The newer run's capture diagnosis is never attached to the prior eligible outcome.
    assert snapshot.latest_outcome.output_error is None and snapshot.latest_outcome.output_preview is None


@pytest.mark.parametrize(
    ("status", "stop_cause"),
    (("failed", "error"), ("interrupted", "shutdown"), ("cancelled", "owner_cancel")),
)
def test_non_completed_terminal_states_are_exact_and_ineligible(monkeypatch: pytest.MonkeyPatch, status: str, stop_cause: str) -> None:
    snapshot = _snapshot(monkeypatch, _event(_fact("run-B", status, stop_cause=stop_cause), sequence=7))

    assert snapshot.latest_outcome is None
    run = snapshot.terminal_run
    assert run is not None
    assert (run.run_id, run.status, run.stop_cause) == ("run-B", status, stop_cause)
    assert run.capture == "not_completed" and not run.eligible


def test_completed_empty_run_is_an_exact_terminal_run(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(monkeypatch, _event(_fact("run-B", "completed", output="", output_bytes=0), sequence=4))

    assert snapshot.latest_outcome is None
    run = snapshot.terminal_run
    assert run is not None
    assert (run.run_id, run.capture, run.eligible) == ("run-B", "empty", False)


def test_eligible_terminal_run_keeps_its_artifact_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(
        monkeypatch,
        _event(
            _fact("run-A", "completed", output_preview="bounded", output_sha256=_DIGEST_A, output_bytes=90_000),
            sequence=4,
        ),
    )

    assert snapshot.latest_outcome is not None and snapshot.latest_outcome.run_id == "run-A"
    run = snapshot.terminal_run
    assert run is not None
    assert (run.run_id, run.capture, run.eligible) == ("run-A", "artifact", True)
    assert (run.output_sha256, run.output_bytes, run.output_preview) == (_DIGEST_A, 90_000, "bounded")
    assert run.terminal_sequence == 4


def test_capture_failure_is_classified_from_fields_not_error_text(monkeypatch: pytest.MonkeyPatch) -> None:
    cause = "ArtifactStoreError: quota exceeded"
    snapshot = _snapshot(monkeypatch, _event(_fact("run-B", "completed", output_bytes=10, output_error=cause), sequence=3))

    assert snapshot.latest_outcome is None
    run = snapshot.terminal_run
    assert run is not None
    assert (run.capture, run.eligible, run.output_error) == ("capture_failed", False, cause)


def test_no_committed_terminal_run_yields_no_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(monkeypatch)

    assert snapshot.latest_outcome is None
    assert snapshot.terminal_run is None


def test_terminal_identity_is_stable_across_reads_and_other_child_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = [
        _event(_fact("run-A", "completed", output="accepted", output_sha256=_DIGEST_A, output_bytes=8), sequence=2),
        _event(_fact("run-B", "cancelled", stop_cause="owner_cancel"), sequence=3),
        _event(_fact("other-child", "completed", output="other", child_session_id="other-child"), sequence=8),
    ]
    store = SimpleNamespace(events_by_types=lambda *_args: facts)
    manager = DelegationManager(cast(Any, SimpleNamespace(ensure_open=lambda: None, store=store)))
    monkeypatch.setattr(manager, "list_descendants", lambda _caller: (_node(),))

    first = manager.read_child_snapshot(_PARENT, _CHILD)
    second = manager.read_child_snapshot(_PARENT, _CHILD)
    assert first is not None and second is not None
    assert first.terminal_run is not None and second.terminal_run is not None
    assert first.terminal_run == second.terminal_run
    assert (second.terminal_run.run_id, second.terminal_run.terminal_sequence) == ("run-B", 3)
    assert second.terminal_run.terminal_sequence != second.descendant.last_event_sequence


@pytest.mark.parametrize(
    ("payload", "field"),
    (
        ({"child_session_id": _CHILD, "status": "completed"}, "run_id"),
        ({"child_session_id": _CHILD, "run_id": "run-B"}, "status"),
        ({"child_session_id": _CHILD, "run_id": "run-B", "status": "mystery"}, "status"),
        ({"child_session_id": _CHILD, "run_id": "run-B", "status": "completed", "output_preview": 7}, "output_preview"),
        ({"child_session_id": _CHILD, "run_id": "run-B", "status": "completed", "output_sha256": 7}, "output_sha256"),
        ({"child_session_id": _CHILD, "run_id": "run-B", "status": "completed", "stop_cause": 7}, "stop_cause"),
        ({"child_session_id": _CHILD, "run_id": "run-B", "status": "completed", "output_bytes": True}, "output_bytes"),
    ),
)
def test_malformed_terminal_payload_raises_a_typed_error(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], field: str) -> None:
    with pytest.raises(StoreConflict, match=field):
        _snapshot(monkeypatch, _event(payload, sequence=4))
