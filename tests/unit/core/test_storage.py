"""Tests for core storage: SQLite persistence, preflight, immutability, and atomic CAS."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.core.storage.store import (
    MAX_COMMITTED_EVENT_BYTES,
    Checkpoint,
    EventDraft,
    ForeignDatabaseError,
    SessionRecord,
    SqliteStore,
    StorageError,
    StoreConflict,
    StoredEvent,
    canonical_json,
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_foreign_file_rejection_and_byte_preservation(tmp_path: Path) -> None:
    """Pre-existing empty, foreign, corrupted, or incompatible files must be refused byte-unchanged."""
    # 1. Empty file (0 bytes)
    empty_file = tmp_path / "empty.sqlite3"
    empty_file.touch()
    assert empty_file.stat().st_size == 0
    with pytest.raises(ForeignDatabaseError, match="empty database file"):
        SqliteStore(empty_file)
    assert empty_file.exists()
    assert empty_file.stat().st_size == 0
    assert not (tmp_path / "empty.sqlite3-wal").exists()
    assert not (tmp_path / "empty.sqlite3-shm").exists()

    # 2. Foreign SQLite database without kernel_identity table
    foreign_db = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(foreign_db) as conn:
        conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO other_table VALUES (1, 'user data')")
        conn.commit()
    foreign_sha_before = _file_sha256(foreign_db)
    with pytest.raises(ForeignDatabaseError, match="missing identity"):
        SqliteStore(foreign_db)
    assert _file_sha256(foreign_db) == foreign_sha_before

    # 3. Arbitrary non-SQLite binary/text file
    corrupt_file = tmp_path / "corrupt.sqlite3"
    corrupt_file.write_bytes(b"THIS IS NOT A SQLITE DATABASE FILE 1234567890")
    corrupt_sha_before = _file_sha256(corrupt_file)
    with pytest.raises(ForeignDatabaseError, match="unsupported database file"):
        SqliteStore(corrupt_file)
    assert _file_sha256(corrupt_file) == corrupt_sha_before

    # 4. SQLite database with invalid kernel_identity magic or schema version
    bad_identity_db = tmp_path / "bad_identity.sqlite3"
    with sqlite3.connect(bad_identity_db) as conn:
        conn.execute("CREATE TABLE kernel_identity (id INTEGER PRIMARY KEY, magic TEXT, schema_version INTEGER)")
        conn.execute("INSERT INTO kernel_identity VALUES (1, 'wrong-magic', 1)")
        conn.commit()
    bad_id_sha_before = _file_sha256(bad_identity_db)
    with pytest.raises(ForeignDatabaseError, match="unsupported identity"):
        SqliteStore(bad_identity_db)
    assert _file_sha256(bad_identity_db) == bad_id_sha_before

    # 5. Non-file path (e.g. directory)
    directory_path = tmp_path / "a_directory"
    directory_path.mkdir()
    with pytest.raises(ForeignDatabaseError, match="not a regular file"):
        SqliteStore(directory_path)


def test_supported_identity_missing_root_columns_is_rejected_unchanged(tmp_path: Path) -> None:
    """A same-version database that lacks the root action-chain columns is refused byte-unchanged.

    Root action-chain truth (continuations.root_id, root_budgets) is part of the
    schema version's contract: a file claiming this identity but missing those
    columns must be rejected by the read-only preflight, never migrated.
    """
    # 1. root_budgets without its reservation counter.
    budget_db = tmp_path / "root_budgets_missing.sqlite3"
    store = SqliteStore(budget_db)
    store.close()
    with sqlite3.connect(budget_db) as conn:
        conn.execute("ALTER TABLE root_budgets RENAME TO root_budgets_full")
        conn.execute(
            "CREATE TABLE root_budgets (root_id TEXT PRIMARY KEY, budget INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL) STRICT"
        )
        conn.commit()
    budget_sha_before = _file_sha256(budget_db)
    with pytest.raises(ForeignDatabaseError, match="root_budgets schema"):
        SqliteStore(budget_db)
    assert _file_sha256(budget_db) == budget_sha_before

    # 2. continuations without its root chain reference.
    chain_db = tmp_path / "continuations_missing.sqlite3"
    store = SqliteStore(chain_db)
    store.close()
    with sqlite3.connect(chain_db) as conn:
        conn.execute("ALTER TABLE continuations DROP COLUMN root_id")
        conn.commit()
    chain_sha_before = _file_sha256(chain_db)
    with pytest.raises(ForeignDatabaseError, match="continuations schema"):
        SqliteStore(chain_db)
    assert _file_sha256(chain_db) == chain_sha_before


def test_revision_opaque_ids_and_immutability(tmp_path: Path) -> None:
    """A generation id is opaque; the same id accepts identical bytes and refuses changed ones."""
    db_path = tmp_path / "kernel.sqlite3"
    with SqliteStore(db_path) as store:
        payload_1 = {"b": 2, "a": {"z": 10, "y": 20}}
        payload_2 = {"a": {"y": 20, "z": 10}, "b": 2}

        rev_id_1 = store.put_revision(payload_1)
        rev_id_2 = store.put_revision(payload_2)
        assert rev_id_1 != rev_id_2

        assert store.put_revision(payload_1, revision_id=rev_id_1) == rev_id_1
        with pytest.raises(ValueError, match="already exists with other content"):
            store.put_revision({"b": 3}, revision_id=rev_id_1)

        # Re-fetching returns immutable snapshot
        loaded = store.revision(rev_id_1)
        assert loaded["b"] == 2
        a_val = loaded["a"]
        assert isinstance(a_val, Mapping)
        assert a_val["z"] == 10
        with pytest.raises((TypeError, AttributeError)):
            cast(Any, loaded)["b"] = 999
        with pytest.raises(KeyError, match="unknown revision"):
            store.revision("0" * 64)


def test_immutable_history_triggers(tmp_path: Path) -> None:
    """Database triggers forbid updating or deleting revisions and session_events."""
    db_path = tmp_path / "immutable.sqlite3"
    with SqliteStore(db_path) as store:
        rev_id = store.put_revision({"plugins": []})
        store.record_workspace_revision("/workspace", rev_id)
        store.create_session("sess-imm", "/workspace", rev_id)
        store.commit(
            "sess-imm",
            expected_sequence=0,
            expected_run_id=None,
            run_id="run-1",
            revision_id=rev_id,
            events=(EventDraft("test.event", "source", "actor", {"k": "v"}),),
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"cursor": 1},
        )

    # Open direct connection to attempt trigger violations
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="capability revisions are immutable"):
            conn.execute("UPDATE revisions SET payload_json = '{}'")

        with pytest.raises(sqlite3.IntegrityError, match="capability revisions are immutable"):
            conn.execute("DELETE FROM revisions")

        with pytest.raises(sqlite3.IntegrityError, match="session events are immutable"):
            conn.execute("UPDATE session_events SET payload_json = '{}'")

        with pytest.raises(sqlite3.IntegrityError, match="session events are immutable"):
            conn.execute("DELETE FROM session_events")


def test_session_lifecycle_and_foreign_key_invariants(tmp_path: Path) -> None:
    """Session creation verifies foreign keys, initial fields, and unknown-session lookups."""
    db_path = tmp_path / "lifecycle.sqlite3"
    with SqliteStore(db_path) as store:
        # A session is created on the workspace's recorded revision: a caller holding some
        # other (never adopted) revision is refused instead of being pinned to a dead ref.
        rev_id = store.put_revision({"config": "valid"})
        store.record_workspace_revision("/workspace", rev_id)
        with pytest.raises(StoreConflict):
            store.create_session("sess-bad", "/workspace", "missing-revision")

        session = store.create_session("sess-1", "/workspace", rev_id)

        assert isinstance(session, SessionRecord)
        assert session.session_id == "sess-1"
        assert session.workspace == "/workspace"
        assert session.status == "idle"
        assert session.active_revision_id == rev_id
        assert session.run_id is None
        assert session.turn == 0
        assert session.last_event_sequence == 0

        # Duplicate session fails UNIQUE constraint
        with pytest.raises(sqlite3.IntegrityError):
            store.record_workspace_revision("/workspace", rev_id)
            store.create_session("sess-1", "/workspace", rev_id)

        # Lookup methods
        loaded = store.session("sess-1")
        assert loaded == session

        assert store.checkpoint("sess-1") is None
        assert store.events("sess-1") == ()

        # Unknown session lookups raise KeyError
        with pytest.raises(KeyError, match="unknown session: unknown"):
            store.session("unknown")
        with pytest.raises(KeyError, match="unknown session: unknown"):
            store.checkpoint("unknown")
        with pytest.raises(KeyError, match="unknown session: unknown"):
            store.events("unknown")


def test_commit_atomic_cas_and_stale_writer_protection(tmp_path: Path) -> None:
    """Atomic CAS commit validates sequence, run ID, and active revision, rejecting stale writers."""
    db_path = tmp_path / "cas.sqlite3"
    with SqliteStore(db_path) as store:
        rev_id = store.put_revision({"plugins": ["files"]})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-cas", "/ws", rev_id)

        # 1. First valid commit
        draft_1 = EventDraft("core.turn_start", "core", "user", {"turn": 1})
        draft_2 = EventDraft("tool.call", "core", "model", {"tool": "read"})
        session = store.commit(
            "sess-cas",
            expected_sequence=0,
            expected_run_id=None,
            run_id="run-1",
            revision_id=rev_id,
            events=(draft_1, draft_2),
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"call_index": 0},
            start_turn=True,
        )

        assert session.turn == 1
        assert session.status == "running"
        assert session.run_id == "run-1"
        assert session.last_event_sequence == 2

        events = store.events("sess-cas")
        assert len(events) == 2
        assert isinstance(events[0], StoredEvent)
        assert events[0].sequence == 1
        assert events[0].run_id == "run-1"
        assert events[0].event_type == "core.turn_start"
        assert events[1].sequence == 2

        cp = store.checkpoint("sess-cas")
        assert isinstance(cp, Checkpoint)
        assert cp.kind == "ready"
        assert cp.run_id == "run-1"
        assert cp.last_event_sequence == 2
        assert cp.payload == {"call_index": 0}

        # 2. Advance session to sequence 3
        draft_3 = EventDraft("tool.completed", "host", "tool", {"result": "ok"})
        session = store.commit(
            "sess-cas",
            expected_sequence=2,
            expected_run_id="run-1",
            run_id="run-1",
            revision_id=rev_id,
            events=(draft_3,),
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"call_index": 1},
            start_turn=False,
        )
        assert session.turn == 1
        assert session.last_event_sequence == 3

        # 3. Stale writer: sequence mismatch (writer saw sequence 2, but DB is at 3)
        with pytest.raises(StoreConflict, match="sequence mismatch"):
            store.commit(
                "sess-cas",
                expected_sequence=2,
                expected_run_id="run-1",
                run_id="run-1",
                revision_id=rev_id,
                events=(EventDraft("stale.event", "source", "actor", {}),),
                status="running",
                checkpoint_kind="ready",
                checkpoint_payload={},
            )

        # 4. Stale writer: run ID mismatch (writer expects old or wrong run ID)
        with pytest.raises(StoreConflict, match="run_id mismatch"):
            store.commit(
                "sess-cas",
                expected_sequence=3,
                expected_run_id="wrong-run",
                run_id="run-1",
                revision_id=rev_id,
                events=(),
                status="running",
                checkpoint_kind="ready",
                checkpoint_payload={},
            )

        # 5. Stale writer: active revision mismatch
        rev_id_2 = store.put_revision({"plugins": ["files", "terminal"]})
        with pytest.raises(StoreConflict, match="active revision mismatch"):
            store.commit(
                "sess-cas",
                expected_sequence=3,
                expected_run_id="run-1",
                run_id="run-1",
                revision_id=rev_id_2,
                events=(),
                status="running",
                checkpoint_kind="ready",
                checkpoint_payload={},
            )

        # 6. Non-existent session
        with pytest.raises(StoreConflict, match="does not exist"):
            store.commit(
                "sess-missing",
                expected_sequence=0,
                expected_run_id=None,
                run_id="run-1",
                revision_id=rev_id,
                events=(),
                status="running",
                checkpoint_kind="ready",
                checkpoint_payload={},
            )


def test_atomic_run_rotation_for_session_resume(tmp_path: Path) -> None:
    """A session can atomically rotate run ID from expected old run to new run upon resume."""
    db_path = tmp_path / "resume.sqlite3"
    with SqliteStore(db_path) as store:
        rev_id = store.put_revision({"rev": 1})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-resume", "/ws", rev_id)

        # Run 1 commits and pauses at approval_wait
        store.commit(
            "sess-resume",
            expected_sequence=0,
            expected_run_id=None,
            run_id="run-1",
            revision_id=rev_id,
            events=(EventDraft("tool.approval_requested", "core", "model", {"call_id": "c1"}),),
            status="waiting",
            checkpoint_kind="approval_wait",
            checkpoint_payload={"approval_seq": 1},
            start_turn=True,
        )

        # Resume under Run 2: expected_run_id is 'run-1', run_id rotates to 'run-2'
        session = store.commit(
            "sess-resume",
            expected_sequence=1,
            expected_run_id="run-1",
            run_id="run-2",
            revision_id=rev_id,
            events=(EventDraft("user.approval_granted", "client", "user", {"call_id": "c1"}),),
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"next": 0},
            start_turn=False,
        )
        assert session.run_id == "run-2"
        assert session.last_event_sequence == 2

        # A stale writer with expected_run_id="run-1" is now locked out
        with pytest.raises(StoreConflict, match="run_id mismatch"):
            store.commit(
                "sess-resume",
                expected_sequence=2,
                expected_run_id="run-1",
                run_id="run-1",
                revision_id=rev_id,
                events=(),
                status="running",
                checkpoint_kind="ready",
                checkpoint_payload={},
            )


def test_restart_prefix_and_checkpoint_alignment(tmp_path: Path) -> None:
    """Restarting after crash preserves exact event sequence prefix and checkpoint alignment."""
    db_path = tmp_path / "restart.sqlite3"
    rev_id: str

    with SqliteStore(db_path) as store:
        rev_id = store.put_revision({"version": "p1"})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-rst", "/ws", rev_id)

        events_batch = tuple(EventDraft(f"event.{i}", "core", "model", {"index": i}) for i in range(1, 6))
        store.commit(
            "sess-rst",
            expected_sequence=0,
            expected_run_id=None,
            run_id="run-1",
            revision_id=rev_id,
            events=events_batch,
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"index": 5},
            start_turn=True,
        )

    # Re-open store as if new process started
    with SqliteStore(db_path) as restarted:
        sess = restarted.session("sess-rst")
        cp = restarted.checkpoint("sess-rst")
        assert cp is not None
        assert cp.last_event_sequence == sess.last_event_sequence == 5

        # Querying prefix after sequence 2 yields events 3, 4, 5
        tail_events = restarted.events("sess-rst", after_sequence=2)
        assert len(tail_events) == 3
        assert [e.sequence for e in tail_events] == [3, 4, 5]
        assert [e.payload["index"] for e in tail_events] == [3, 4, 5]

        # Querying after checkpoint sequence yields no events
        assert restarted.events("sess-rst", after_sequence=cp.last_event_sequence) == ()


def test_full_rollback_on_write_failure(tmp_path: Path) -> None:
    """A commit failure midway (e.g. invalid checkpoint kind) rolls back all event appends and session updates."""
    db_path = tmp_path / "rollback.sqlite3"
    with SqliteStore(db_path) as store:
        rev_id = store.put_revision({"v": 1})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-rb", "/ws", rev_id)

        # Baseline commit: 1 event
        store.commit(
            "sess-rb",
            expected_sequence=0,
            expected_run_id=None,
            run_id="run-1",
            revision_id=rev_id,
            events=(EventDraft("initial", "core", "user", {}),),
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"step": 1},
            start_turn=True,
        )

        # Attempt commit with invalid checkpoint kind ('bogus_kind' violates CHECK constraint)
        with pytest.raises(sqlite3.IntegrityError):
            store.commit(
                "sess-rb",
                expected_sequence=1,
                expected_run_id="run-1",
                run_id="run-1",
                revision_id=rev_id,
                events=(
                    EventDraft("ev-2", "core", "model", {}),
                    EventDraft("ev-3", "core", "model", {}),
                ),
                status="running",
                checkpoint_kind="bogus_kind",  # Invalid checkpoint kind
                checkpoint_payload={"step": 2},
            )

        # Verify full rollback: session remains at seq 1, events table only has 1 event
        sess = store.session("sess-rb")
        assert sess.last_event_sequence == 1
        assert len(store.events("sess-rb")) == 1

        cp = store.checkpoint("sess-rb")
        assert cp is not None
        assert cp.last_event_sequence == 1
        assert cp.payload == {"step": 1}

        # Next valid commit succeeds cleanly
        store.commit(
            "sess-rb",
            expected_sequence=1,
            expected_run_id="run-1",
            run_id="run-1",
            revision_id=rev_id,
            events=(EventDraft("ev-2-valid", "core", "model", {}),),
            status="completed",
            checkpoint_kind="terminal",
            checkpoint_payload={"done": True},
        )
        assert store.session("sess-rb").last_event_sequence == 2
        assert len(store.events("sess-rb")) == 2


def test_store_close_and_context_manager(tmp_path: Path) -> None:
    """Closing the store terminates its SQLite connection and refuses further operations."""
    db_path = tmp_path / "closed.sqlite3"
    store = SqliteStore(db_path)
    rev_id = store.put_revision({"a": 1})
    store.record_workspace_revision("/ws", rev_id)
    store.create_session("sess-c", "/ws", rev_id)
    store.close()

    with pytest.raises(StorageError, match="store is closed"):
        store.put_revision({"a": 2})

    with pytest.raises(StorageError, match="store is closed"):
        store.session("sess-c")

    with pytest.raises(StorageError, match="store is closed"):
        store.events("sess-c")

    with pytest.raises(StorageError, match="store is closed"):
        store.checkpoint("sess-c")

    # Double close is safe
    store.close()


def test_store_can_commit_from_a_run_worker_thread(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "worker.sqlite3") as store:
        revision_id = store.put_revision({"plugin": "read"})
        store.record_workspace_revision("/workspace", revision_id)
        store.create_session("worker", "/workspace", revision_id)
        with ThreadPoolExecutor(max_workers=1) as worker:
            committed = worker.submit(
                store.commit,
                "worker",
                expected_sequence=0,
                expected_run_id=None,
                run_id="run",
                revision_id=revision_id,
                events=(EventDraft("observation", "tool", "model", {"value": 42}),),
                status="completed",
                checkpoint_kind="terminal",
                checkpoint_payload={"last": 42},
            ).result(timeout=5)
        assert committed.last_event_sequence == 1
        assert store.events("worker")[0].payload["value"] == 42
        cp = store.checkpoint("worker")
        assert cp is not None
        assert cp.payload["last"] == 42


def _payload_of_size(size: int) -> dict[str, object]:
    """Build a payload whose canonical JSON is exactly `size` bytes (`{"pad":"<pad>"}`)."""
    pad = size - len('{"pad":""}')
    assert pad >= 0
    return {"pad": "x" * pad}


def test_commit_staged_rejects_oversized_event_payload_atomically(tmp_path: Path) -> None:
    """An event past the committed-event bound fails the whole commit visibly, writing nothing."""
    payload = _payload_of_size(MAX_COMMITTED_EVENT_BYTES + 1)
    assert len(canonical_json(payload).encode("utf-8")) == MAX_COMMITTED_EVENT_BYTES + 1
    with SqliteStore(tmp_path / "guard.sqlite3") as store:
        rev_id = store.put_revision({"plugins": ["files"]})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-guard", "/ws", rev_id)
        store.commit_staged(
            "sess-guard",
            run_id="run-1",
            revision_id=rev_id,
            events=(EventDraft("core.turn_start", "core", "user", {"turn": 1}),),
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"call_index": 0},
        )
        with pytest.raises(StoreConflict, match=f"{MAX_COMMITTED_EVENT_BYTES + 1} bytes.*{MAX_COMMITTED_EVENT_BYTES}-byte"):
            store.commit_staged(
                "sess-guard",
                expected_sequence=1,
                expected_run_id="run-1",
                run_id="run-1",
                revision_id=rev_id,
                events=(EventDraft("tool.completed", "host", "tool", payload),),
                status="running",
                checkpoint_kind="ready",
                checkpoint_payload={"call_index": 1},
            )
        assert store.session("sess-guard").last_event_sequence == 1
        assert len(store.events("sess-guard")) == 1
        cp = store.checkpoint("sess-guard")
        assert cp is not None
        assert cp.payload == {"call_index": 0}


def test_commit_accepts_event_payload_at_bound(tmp_path: Path) -> None:
    """A payload of exactly the bound commits through the `commit` wrapper unchanged."""
    payload = _payload_of_size(MAX_COMMITTED_EVENT_BYTES)
    assert len(canonical_json(payload).encode("utf-8")) == MAX_COMMITTED_EVENT_BYTES
    with SqliteStore(tmp_path / "bound.sqlite3") as store:
        rev_id = store.put_revision({"plugins": ["files"]})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-bound", "/ws", rev_id)
        session = store.commit(
            "sess-bound",
            expected_sequence=0,
            expected_run_id=None,
            run_id="run-1",
            revision_id=rev_id,
            events=(EventDraft("tool.completed", "host", "tool", payload),),
            status="running",
            checkpoint_kind="ready",
            checkpoint_payload={"call_index": 0},
        )
        assert session.last_event_sequence == 1
        assert store.events("sess-bound")[0].payload == payload
