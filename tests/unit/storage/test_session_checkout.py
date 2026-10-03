"""Storage-level checkout: move the leaf, never touch the log.

Checkout is a position change on ``sessions.leaf_sequence``. The behavior that
matters is what it does *not* do: the abandoned continuation stays in
``session_events`` and stays reachable by checking out again. The path walk and
its refusals (missing ancestor, cycle, split interaction) are the same
invariants the fork boundary enforces, so they are asserted on the real store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.contracts import (
    RuntimeSessionCheckoutBoundaryError,
    SessionTreePathError,
)
from voidcode.runtime.events import EventEnvelope, EventSource
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.storage.sessions import SessionTreeEvent, session_event_path


def _seed_store(tmp_path: Path) -> tuple[SqliteSessionStore, Path]:
    return SqliteSessionStore(database_path=tmp_path / "checkout.sqlite3"), tmp_path


def _seed_session(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    session_id: str,
    events: tuple[tuple[str, str, dict[str, object]], ...],
) -> None:
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=session_id,
        prompt="run",
        session_metadata={"workspace": str(workspace), "runtime_state": {"run_id": "stale-run"}},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    store.append_session_events(
        workspace=workspace,
        session_id=session_id,
        events=tuple((event_type, cast(EventSource, source), payload, None) for event_type, source, payload in events),
    )


def _rows(database_path: Path, session_id: str) -> list[tuple[int, int | None]]:
    with sqlite3.connect(database_path) as connection:
        return [
            (row[0], row[1])
            for row in connection.execute(
                "SELECT sequence, parent_sequence FROM session_events WHERE session_id = ? ORDER BY sequence ASC",
                (session_id,),
            )
        ]


def _leaf(database_path: Path, session_id: str) -> int | None:
    with sqlite3.connect(database_path) as connection:
        return connection.execute("SELECT leaf_sequence FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]


def _envelope(sequence: int, parent: int | None) -> EventEnvelope:
    return EventEnvelope(session_id="s", sequence=sequence, event_type="runtime.tool_completed", source=cast(EventSource, "runtime"))


def test_checkout_moves_leaf_and_keeps_the_abandoned_events(tmp_path: Path) -> None:
    store, workspace = _seed_store(tmp_path)
    events = (
        ("runtime.request_received", "runtime", {"prompt": "run"}),
        ("runtime.tool_started", "runtime", {"tool": "read", "tool_call_id": "call-1"}),
        ("runtime.tool_completed", "tool", {"tool": "read", "tool_call_id": "call-1", "status": "ok"}),
        ("graph.response_ready", "graph", {"summary": "done"}),
    )
    _seed_session(store, workspace=workspace, session_id="s1", events=events)
    before = _rows(store._resolve_database_path(), "s1")
    assert before == [(1, None), (2, 1), (3, 2), (4, 3)]
    assert _leaf(store._resolve_database_path(), "s1") == 4

    assert store.checkout_session(workspace=workspace, session_id="s1", sequence=1) == 1

    assert _leaf(store._resolve_database_path(), "s1") == 1
    # Nothing was deleted or rewritten; only the leaf moved.
    assert _rows(store._resolve_database_path(), "s1") == before
    assert [event.sequence for event in store.session_path(workspace=workspace, session_id="s1")] == [1]

    # The abandoned branch is still reachable: check back out to its tip.
    assert store.checkout_session(workspace=workspace, session_id="s1", sequence=4) == 4
    assert _leaf(store._resolve_database_path(), "s1") == 4
    assert _rows(store._resolve_database_path(), "s1") == before
    assert [event.sequence for event in store.session_path(workspace=workspace, session_id="s1")] == [1, 2, 3, 4]


def test_event_pages_pin_a_branch_and_keep_the_durable_watermark(tmp_path: Path) -> None:
    store, workspace = _seed_store(tmp_path)
    _seed_session(
        store,
        workspace=workspace,
        session_id="paged",
        events=(
            ("runtime.request_received", "runtime", {"prompt": "run"}),
            ("runtime.tool_started", "runtime", {"tool": "read", "tool_call_id": "call-1"}),
            ("runtime.tool_completed", "tool", {"tool": "read", "tool_call_id": "call-1", "status": "ok"}),
            ("graph.response_ready", "graph", {"summary": "original"}),
        ),
    )
    store.checkout_session(workspace=workspace, session_id="paged", sequence=1)
    store.append_session_events(
        workspace=workspace,
        session_id="paged",
        events=(("graph.response_ready", cast(EventSource, "graph"), {"summary": "new branch"}, None),),
    )

    first = store.read_session_event_page(
        workspace=workspace,
        session_id="paged",
        after_sequence=0,
        limit=2,
        leaf_sequence=4,
    )
    assert first.leaf_sequence == 4
    assert first.max_sequence == 5
    assert [entry.event.sequence for entry in first.entries] == [1, 2]
    assert [entry.parent_sequence for entry in first.entries] == [None, 1]
    assert first.next_after_sequence == 2

    second = store.read_session_event_page(
        workspace=workspace,
        session_id="paged",
        after_sequence=2,
        limit=2,
        leaf_sequence=4,
    )
    assert [entry.event.sequence for entry in second.entries] == [3, 4]
    assert second.next_after_sequence is None
    assert [entry.parent_sequence for entry in second.entries] == [2, 3]

    current = store.read_session_event_page(
        workspace=workspace,
        session_id="paged",
        after_sequence=0,
        limit=10,
    )
    assert current.leaf_sequence == 5
    assert current.max_sequence == 5
    assert [entry.event.sequence for entry in current.entries] == [1, 5]
    with pytest.raises(ValueError, match="selected session path"):
        store.read_session_event_page(
            workspace=workspace,
            session_id="paged",
            after_sequence=2,
            limit=2,
            leaf_sequence=5,
        )
    with pytest.raises(
        SessionTreePathError,
        match="leaf exceeds its event watermark",
    ):
        store.read_session_event_page(
            workspace=workspace,
            session_id="paged",
            after_sequence=0,
            limit=2,
            leaf_sequence=9,
        )
    _seed_session(store, workspace=workspace, session_id="empty", events=())
    empty = store.read_session_event_page(
        workspace=workspace,
        session_id="empty",
        after_sequence=0,
        limit=1,
    )
    assert empty.leaf_sequence is None
    assert empty.max_sequence == 0
    assert empty.entries == ()
    assert empty.next_after_sequence is None


def test_checkout_refuses_unknown_sequence(tmp_path: Path) -> None:
    store, workspace = _seed_store(tmp_path)
    _seed_session(
        store,
        workspace=workspace,
        session_id="s2",
        events=(("runtime.request_received", "runtime", {"prompt": "run"}),),
    )
    with pytest.raises(ValueError, match="has no event sequence 9"):
        store.checkout_session(workspace=workspace, session_id="s2", sequence=9)
    assert _leaf(store._resolve_database_path(), "s2") == 1


def test_checkout_refuses_path_that_splits_a_tool_pair(tmp_path: Path) -> None:
    store, workspace = _seed_store(tmp_path)
    events = (
        ("runtime.request_received", "runtime", {"prompt": "run"}),
        ("runtime.tool_started", "runtime", {"tool": "read", "tool_call_id": "call-1"}),
        ("runtime.tool_completed", "tool", {"tool": "read", "tool_call_id": "call-1", "status": "ok"}),
    )
    _seed_session(store, workspace=workspace, session_id="s3", events=events)
    with pytest.raises(RuntimeSessionCheckoutBoundaryError, match="splits a tool call"):
        store.checkout_session(workspace=workspace, session_id="s3", sequence=2)
    assert _leaf(store._resolve_database_path(), "s3") == 3

    # The completed pair is a legal target and moves the leaf.
    assert store.checkout_session(workspace=workspace, session_id="s3", sequence=3) == 3


def test_checkout_clears_position_scoped_cached_state(tmp_path: Path) -> None:
    store, workspace = _seed_store(tmp_path)
    _seed_session(
        store,
        workspace=workspace,
        session_id="s4",
        events=(("runtime.request_received", "runtime", {"prompt": "run"}),),
    )
    database_path = store._resolve_database_path()
    approval_payload = '{"request_id": "req-1"}'
    checkpoint_payload = json.dumps(
        {
            "kind": "approval_wait",
            "prompt": "run",
            "session_metadata": {},
            "tool_results": [],
            "last_event_sequence": 1,
            "output": None,
            "session_status": "waiting",
        }
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE sessions SET pending_approval_json = ?, pending_question_json = ?, resume_checkpoint_json = ? WHERE session_id = 's4'",
            (approval_payload, '{"request_id": "req-2"}', checkpoint_payload),
        )
        connection.commit()

    store.checkout_session(workspace=workspace, session_id="s4", sequence=1)

    with sqlite3.connect(database_path) as connection:
        metadata_json, approval, question, checkpoint = connection.execute(
            "SELECT metadata_json, pending_approval_json, pending_question_json, resume_checkpoint_json FROM sessions WHERE session_id = 's4'"
        ).fetchone()
    metadata = json.loads(metadata_json)
    assert "runtime_state" not in metadata
    assert metadata["workspace"] == str(workspace)
    # The abandoned branch's pending interaction is gone; the stale checkpoint
    # is replaced by one naming the checked-out position, not left absent —
    # otherwise a resume would replay the response this checkout invalidated.
    assert (approval, question) == (None, None)
    assert json.loads(checkpoint)["last_event_sequence"] == 1


def test_session_event_path_walks_root_to_leaf() -> None:
    entries = (
        SessionTreeEvent(event=_envelope(1, None), parent_sequence=None),
        SessionTreeEvent(event=_envelope(2, 1), parent_sequence=1),
        SessionTreeEvent(event=_envelope(3, 2), parent_sequence=2),
        # A sibling branch off event 1: not on the target's path.
        SessionTreeEvent(event=_envelope(4, 1), parent_sequence=1),
    )
    assert [event.sequence for event in session_event_path(entries, target_sequence=3)] == [1, 2, 3]
    assert [event.sequence for event in session_event_path(entries, leaf_sequence=4)] == [1, 4]


def test_session_event_path_refuses_missing_ancestor_and_cycle() -> None:
    missing = (
        SessionTreeEvent(event=_envelope(1, None), parent_sequence=None),
        SessionTreeEvent(event=_envelope(2, 7), parent_sequence=7),
    )
    with pytest.raises(SessionTreePathError, match="sequence 7 is missing"):
        session_event_path(missing, target_sequence=2)

    cycle = (
        SessionTreeEvent(event=_envelope(1, 2), parent_sequence=2),
        SessionTreeEvent(event=_envelope(2, 1), parent_sequence=1),
    )
    with pytest.raises(SessionTreePathError, match="cycle"):
        session_event_path(cycle, target_sequence=2)

    with pytest.raises(SessionTreePathError, match="no leaf"):
        session_event_path((), leaf_sequence=None)


def _status_and_checkpoint(database_path: Path, session_id: str) -> tuple[str, dict[str, object] | None]:
    with sqlite3.connect(database_path) as connection:
        status, raw = connection.execute(
            "SELECT status, resume_checkpoint_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return status, (json.loads(raw) if raw is not None else None)


def test_checkout_makes_a_completed_session_continuable(tmp_path: Path) -> None:
    """A checkout can never leave the session terminal at the chosen position.

    ``sessions resume`` replays a stored response while the row is sealed, so a
    checkout of a completed session must un-seal it *and* record a checkpoint at
    the new position: otherwise the resume would replay a turn the checkout just
    invalidated.
    """
    store, workspace = _seed_store(tmp_path)
    _seed_session(
        store,
        workspace=workspace,
        session_id="s6",
        events=(
            ("runtime.request_received", "runtime", {"prompt": "A"}),
            ("graph.response_ready", "graph", {"summary": "A done"}),
            ("runtime.request_received", "runtime", {"prompt": "B"}),
            ("graph.response_ready", "graph", {"summary": "B done"}),
        ),
    )
    database_path = store._resolve_database_path()
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE sessions SET status = 'completed' WHERE session_id = 's6'")
        connection.commit()
    assert _status_and_checkpoint(database_path, "s6")[0] == "completed"

    assert store.checkout_session(workspace=workspace, session_id="s6", sequence=2) == 2

    status, checkpoint = _status_and_checkpoint(database_path, "s6")
    assert status == "interrupted"
    assert checkpoint is not None
    assert checkpoint["kind"] == "interrupted"
    # The checkpoint names the checked-out position itself, not the row
    # watermark: a resume restores the leaf to it and never deletes the tail
    # (the abandoned branch beyond it must survive). The prompt names the last
    # request on the new path.
    assert checkpoint["last_event_sequence"] == 2
    assert checkpoint["prompt"] == "A"


def test_session_entries_mark_abandoned_rows_and_preview_their_text(tmp_path: Path) -> None:
    store, workspace = _seed_store(tmp_path)
    _seed_session(
        store,
        workspace=workspace,
        session_id="s7",
        events=(
            ("runtime.request_received", "runtime", {"prompt": "keep me"}),
            ("runtime.request_received", "runtime", {"prompt": "abandon me"}),
        ),
    )
    store.checkout_session(workspace=workspace, session_id="s7", sequence=1)

    entries = store.session_entries(workspace=workspace, session_id="s7")
    assert [(entry.sequence, entry.event_type, entry.parent_sequence) for entry in entries] == [
        (1, "runtime.request_received", None),
        (2, "runtime.request_received", 1),
    ]
    assert [entry.on_current_path for entry in entries] == [True, False]
    assert [entry.preview for entry in entries] == ["keep me", "abandon me"]


def test_session_entries_are_empty_for_a_session_with_no_events(tmp_path: Path) -> None:
    store, workspace = _seed_store(tmp_path)
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id="s8",
        prompt="empty",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    assert store.session_entries(workspace=workspace, session_id="s8") == ()
