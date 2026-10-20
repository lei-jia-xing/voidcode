"""Behavior tests for generation adoption: one atomic revision, pointer, and state move.

Each case drives the real store: a workspace adopted on one revision is adopted onto another, and
the assertions check the atomic unit (new revision row, old payload untouched, pointer plus every
session and continuation retargeted, state copies applied with their versions, checkpoints
retargeted, blockers refusing before anything moves) and the single supported schema rebuild.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

import pytest

from voidcode.kernel.storage.store import (
    SCHEMA_MAGIC,
    SCHEMA_VERSION,
    EventDraft,
    ForeignDatabaseError,
    SqliteStore,
    StagedStateUpdate,
    StoreConflict,
    revision_identity,
)

WORKSPACE = "/tmp/adoption-workspace"


def _v4_schema_sql() -> str:
    """The historical version-4 definition: today's schema with the version-4 identity check.

    The 4 -> 5 transition changes nothing else, so replacing the check rebuilds byte for byte
    the database shape the pre-P5 kernel wrote.
    """
    text = files("voidcode.kernel.storage").joinpath("schema.sql").read_text(encoding="utf-8")
    assert "CHECK (schema_version = 5)" in text
    return text.replace("CHECK (schema_version = 5)", "CHECK (schema_version = 4)").replace("Kernel schema v5.", "Kernel schema v4.", 1)


def _v4_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_v4_schema_sql())
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO kernel_identity (id, magic, schema_version) VALUES (1, ?, ?)",
            (SCHEMA_MAGIC, 4),
        )
        connection.commit()
    finally:
        connection.close()


def _revision(*, image: str, sdk: str, plugins: Mapping[str, object] | None = None) -> dict[str, object]:
    return {
        "sdk_protocol": 1,
        "sdk_sha256": sdk,
        "image": {"digest": image, "kernel_version": "0.1", "launch_protocol": 1},
        "python": "cpython-312",
        "dependencies": {},
        "plugins": dict(plugins or {}),
        "private_config_sha256": "snapshot",
    }


def _adopted(store: SqliteStore, payload: Mapping[str, object]) -> str:
    revision_id = store.put_revision(payload)
    store.record_workspace_revision(WORKSPACE, revision_id)
    return revision_id


def _session(store: SqliteStore, session_id: str, revision_id: str, *, status: str = "idle") -> None:
    store.create_session(session_id, WORKSPACE, revision_id)
    if status != "idle":
        store.commit(
            session_id,
            expected_sequence=0,
            expected_run_id=None,
            run_id=f"run-{session_id}",
            revision_id=revision_id,
            events=(EventDraft("kernel.note", "kernel", "kernel", {}),),
            status=status,
            checkpoint_kind="ready",
            checkpoint_payload={"cursor": "carried"},
        )


def test_adopt_revision_moves_pointer_refs_copies_and_checkpoint_atomically(tmp_path: Path) -> None:
    database = tmp_path / "kernel-v1.sqlite3"
    with SqliteStore(database) as store:
        old_payload = _revision(image="image-old", sdk="sdk-old")
        old_revision = _adopted(store, old_payload)
        _session(store, "acting", old_revision)
        _session(store, "other", old_revision)
        store.enqueue_continuation(
            "continuation-1",
            "owner",
            session_id="other",
            source="test",
            run_id="run-1",
            revision_id=old_revision,
            payload={},
            idempotency_key=None,
        )
        acting = store.session("acting")
        assert acting.status == "completed" or acting.status == "idle"

        new_payload = _revision(image="image-new", sdk="sdk-new")
        target_revision = revision_identity(new_payload)
        assert target_revision != old_revision

        returned = store.adopt_revision(
            workspace=WORKSPACE,
            session_id="acting",
            expected_sequence=acting.last_event_sequence,
            expected_run_id=acting.run_id,
            expected_revision_id=old_revision,
            target_revision_id=target_revision,
            target_payload=new_payload,
            run_id=acting.run_id or "run-acting",
            status=acting.status,
            events=(EventDraft("kernel.revision_adopted", "kernel", "kernel", {"to_revision_id": target_revision}),),
            checkpoint_kind="ready",
            checkpoint_payload={"cursor": "carried"},
            state_updates=(
                StagedStateUpdate(
                    owner="owner",
                    scope="session",
                    scope_key="acting",
                    payload={"count": 1},
                    expected_version=0,
                    schema_version=1,
                    revision_id=target_revision,
                ),
            ),
            require_quiescent=True,
        )

    assert returned == target_revision
    with SqliteStore(database) as store:
        assert store.workspace_current(WORKSPACE) == target_revision
        assert store.session("acting").active_revision_id == target_revision
        assert store.session("other").active_revision_id == target_revision
        assert store.revision(old_revision) == old_payload, "the old revision row is never mutated"
        checkpoint = store.checkpoint("acting")
        assert checkpoint is not None and checkpoint.revision_id == target_revision
        assert checkpoint.payload["cursor"] == "carried"
        types = [event.event_type for event in store.events("acting")]
        assert "kernel.revision_adopted" in types
        rows = store.plugin_state_rows("session", "acting")
        assert [(row.owner, row.schema_version, row.revision_id) for row in rows] == [("owner", 1, target_revision)]
        claimed = store.pending_continuations("other", limit=10)
        assert claimed and all(record.revision_id == target_revision for record in claimed)


def test_adopt_revision_refuses_blockers_and_cas_mismatches_without_moving_anything(tmp_path: Path) -> None:
    database = tmp_path / "kernel-v1.sqlite3"
    with SqliteStore(database) as store:
        old_revision = _adopted(store, _revision(image="image-old", sdk="sdk-old"))
        _session(store, "acting", old_revision)
        _session(store, "busy", old_revision, status="running")
        acting = store.session("acting")
        target = revision_identity(_revision(image="image-new", sdk="sdk-new"))

        with pytest.raises(StoreConflict, match="adoption blocked"):
            store.adopt_revision(
                workspace=WORKSPACE,
                session_id="acting",
                expected_sequence=acting.last_event_sequence,
                expected_run_id=acting.run_id,
                expected_revision_id=old_revision,
                target_revision_id=target,
                target_payload=_revision(image="image-new", sdk="sdk-new"),
                run_id=acting.run_id or "run",
                status=acting.status,
                require_quiescent=True,
            )
        with pytest.raises(StoreConflict, match="sequence mismatch"):
            store.adopt_revision(
                workspace=WORKSPACE,
                session_id="acting",
                expected_sequence=acting.last_event_sequence + 1,
                expected_run_id=acting.run_id,
                expected_revision_id=old_revision,
                target_revision_id=target,
                target_payload=_revision(image="image-new", sdk="sdk-new"),
                run_id=acting.run_id or "run",
                status=acting.status,
                require_quiescent=False,
            )

        assert store.workspace_current(WORKSPACE) == old_revision
        with pytest.raises(KeyError):
            store.revision(target)


def test_adopt_revision_applies_the_single_supported_schema_rebuild(tmp_path: Path) -> None:
    database = tmp_path / "kernel-v1.sqlite3"
    _v4_database(database)
    with SqliteStore(database, allow_schema=frozenset({4, 5})) as store:
        assert store.recorded_schema == 4
        old_revision = _adopted(store, _revision(image="image-old", sdk="sdk-old"))
        _session(store, "acting", old_revision)
        acting = store.session("acting")
        target = revision_identity(_revision(image="image-new", sdk="sdk-new"))

        store.adopt_revision(
            workspace=WORKSPACE,
            session_id="acting",
            expected_sequence=acting.last_event_sequence,
            expected_run_id=acting.run_id,
            expected_revision_id=old_revision,
            target_revision_id=target,
            target_payload=_revision(image="image-new", sdk="sdk-new"),
            run_id=acting.run_id or "run",
            status=acting.status,
            events=(EventDraft("kernel.revision_adopted", "kernel", "kernel", {"to_revision_id": target}),),
            require_quiescent=True,
        )
        assert store.recorded_schema == SCHEMA_VERSION
        assert store.workspace_current(WORKSPACE) == target

    with SqliteStore(database) as store:
        assert store.recorded_schema == SCHEMA_VERSION
        assert store.workspace_current(WORKSPACE) == target
        assert [event.event_type for event in store.events("acting")] == ["kernel.revision_adopted"]


def test_adopt_revision_without_an_acting_session_needs_an_empty_workspace(tmp_path: Path) -> None:
    database = tmp_path / "kernel-v1.sqlite3"
    with SqliteStore(database) as store:
        old_revision = _adopted(store, _revision(image="image-old", sdk="sdk-old"))
        target = revision_identity(_revision(image="image-new", sdk="sdk-new"))

        returned = store.adopt_revision(
            workspace=WORKSPACE,
            session_id=None,
            expected_sequence=None,
            expected_run_id=None,
            expected_revision_id=old_revision,
            target_revision_id=target,
            target_payload=_revision(image="image-new", sdk="sdk-new"),
            run_id="run-adopt",
            status=None,
            require_quiescent=True,
        )
        assert returned == target
        assert store.workspace_current(WORKSPACE) == target

    occupied = tmp_path / "occupied.sqlite3"
    with SqliteStore(occupied) as store:
        old_revision = _adopted(store, _revision(image="image-old", sdk="sdk-old"))
        _session(store, "someone", old_revision)
        with pytest.raises(StoreConflict, match="no sessions"):
            store.adopt_revision(
                workspace=WORKSPACE,
                session_id=None,
                expected_sequence=None,
                expected_run_id=None,
                expected_revision_id=old_revision,
                target_revision_id=revision_identity(_revision(image="image-new", sdk="sdk-new")),
                target_payload=_revision(image="image-new", sdk="sdk-new"),
                run_id="run-adopt",
                status=None,
                require_quiescent=True,
            )


def test_version_locked_opens_refuse_other_versions_and_foreign_files(tmp_path: Path) -> None:
    database = tmp_path / "kernel-v1.sqlite3"
    _v4_database(database)
    with pytest.raises(ForeignDatabaseError, match="unsupported database identity"):
        SqliteStore(database)
    with SqliteStore(database, allow_schema=frozenset({4, 5})) as store:
        assert store.recorded_schema == 4

    foreign = tmp_path / "foreign.sqlite3"
    foreign.write_bytes(b"not a database")
    with pytest.raises(ForeignDatabaseError):
        SqliteStore(foreign)


def test_second_adoption_on_schema_5_runs_no_ddl(tmp_path: Path) -> None:
    database = tmp_path / "kernel-v1.sqlite3"
    with SqliteStore(database) as store:
        first = _adopted(store, _revision(image="image-one", sdk="sdk"))
        _session(store, "acting", first)
        acting = store.session("acting")
        second_payload = _revision(image="image-two", sdk="sdk")
        second = revision_identity(second_payload)
        store.adopt_revision(
            workspace=WORKSPACE,
            session_id="acting",
            expected_sequence=acting.last_event_sequence,
            expected_run_id=acting.run_id,
            expected_revision_id=first,
            target_revision_id=second,
            target_payload=second_payload,
            run_id=acting.run_id or "run",
            status=acting.status,
            require_quiescent=True,
        )
        acting = store.session("acting")
        third_payload = _revision(image="image-three", sdk="sdk")
        third = revision_identity(third_payload)
        store.adopt_revision(
            workspace=WORKSPACE,
            session_id="acting",
            expected_sequence=acting.last_event_sequence,
            expected_run_id=acting.run_id,
            expected_revision_id=second,
            target_revision_id=third,
            target_payload=third_payload,
            run_id=acting.run_id or "run",
            status=acting.status,
            require_quiescent=True,
        )
        assert store.workspace_current(WORKSPACE) == third
        tables = {row[0] for row in store._connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "kernel_identity_v5" not in tables
        definition = store._connection.execute("SELECT sql FROM sqlite_master WHERE name='kernel_identity'").fetchone()[0]
        assert "CHECK (schema_version = 5)" in definition


def test_show_rescues_a_pre_transition_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.kernel.__main__ import main as _main

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / "kernel-v1.sqlite3"
    _v4_database(database)
    name = str(workspace.resolve())
    with SqliteStore(database, allow_schema=frozenset({4, 5})) as store:
        revision_id = store.put_revision(_revision(image="image-old", sdk="sdk-old"))
        store.record_workspace_revision(name, revision_id)
        store.create_session("rescue-me", name, revision_id)

    code = _main(["show", "rescue-me", "--workspace", name, "--state-dir", str(state_dir), "--events"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "rescue-me" and payload["revision_id"] == revision_id
    assert payload["events"] == []
