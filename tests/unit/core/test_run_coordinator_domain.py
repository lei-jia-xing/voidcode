"""Unit tests for run coordinator domain (RunCoordinator, _reserved_cancelled)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from voidcode.core.cancellation import Cancellation
from voidcode.core.engine.run_coordinator import RunCoordinator, _reserved_cancelled
from voidcode.core.engine.turn_execution import CoreResult
from voidcode.core.events import EventType
from voidcode.core.storage.store import SessionRecord, SessionStatus, StoredEvent


def test_reserved_cancelled_helper() -> None:
    # Event with RUN_INTERRUPTED and owner_cancel
    ev1 = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.RUN_INTERRUPTED,
        source="core",
        actor="system",
        revision_id="rev1",
        payload={"stop_cause": "owner_cancel"},
    )
    assert _reserved_cancelled([ev1]) is True

    # Event with RUN_INTERRUPTED but different cause
    ev2 = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.RUN_INTERRUPTED,
        source="core",
        actor="system",
        revision_id="rev1",
        payload={"stop_cause": "user_stop"},
    )
    assert _reserved_cancelled([ev2]) is False

    # Event of another type
    ev3 = StoredEvent(
        session_id="s1",
        sequence=1,
        run_id="r1",
        event_type=EventType.RUN_COMPLETED,
        source="core",
        actor="system",
        revision_id="rev1",
        payload={},
    )
    assert _reserved_cancelled([ev3]) is False


def test_run_coordinator_run_session_busy() -> None:
    host = MagicMock()
    host.closed = False
    host.revision_id = "rev-1"
    host.admits_fresh_work.return_value = False

    @contextmanager
    def mock_lock(sid: str) -> Generator[None]:
        yield

    host.session_lock.side_effect = mock_lock
    coordinator = RunCoordinator(host)

    sess = SessionRecord(
        session_id="s1",
        workspace="/tmp/ws",
        status=SessionStatus.INTERRUPTED,
        active_revision_id="rev-1",
        run_id="r0",
        turn=1,
        last_event_sequence=1,
    )
    host.store_session.return_value = sess

    with pytest.raises(ValueError, match="unfinished session requires explicit resume"):
        coordinator.run("hello", session_id="s1")


def test_run_coordinator_run_success() -> None:
    host = MagicMock()
    host.closed = False
    host.revision_id = "rev-1"
    host.default_abort = Cancellation()
    host.admits_fresh_work.return_value = True

    @contextmanager
    def mock_lock(sid: str) -> Generator[None]:
        yield

    host.session_lock.side_effect = mock_lock
    coordinator = RunCoordinator(host)

    sess = SessionRecord(
        session_id="s1",
        workspace="/tmp/ws",
        status=SessionStatus.IDLE,
        active_revision_id="rev-1",
        run_id=None,
        turn=0,
        last_event_sequence=0,
    )
    host.store_session.return_value = sess
    host.store_events.return_value = ()
    host.drive.return_value = CoreResult(
        session_id="s1",
        run_id="r1",
        revision_id="rev-1",
        status="ok",
        output="done",
    )

    result = coordinator.run("hello", session_id="s1")
    assert result.status == "ok"
    assert result.output == "done"
    host.register_run.assert_called_once()
    host.unregister_run.assert_called_once()
