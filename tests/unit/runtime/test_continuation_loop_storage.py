from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.task import (
    ContinuationLoopRef,
    ContinuationLoopState,
    validate_continuation_loop_id,
)


def _loop(
    *,
    loop_id: str = "loop-1",
    prompt: str = "finish the migration",
    intensive: bool = True,
) -> ContinuationLoopState:
    return ContinuationLoopState(
        loop=ContinuationLoopRef(id=loop_id),
        prompt=prompt,
        completion_promise="DONE",
        max_iterations=3,
        intensive=intensive,
        verification_status="pending" if intensive else "not_required",
    )


def test_validate_continuation_loop_id_rejects_empty_and_slash() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_continuation_loop_id("")

    with pytest.raises(ValueError, match="must not contain '/'"):
        validate_continuation_loop_id("loop/1")


def test_continuation_loop_storage_create_load_and_list(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    loop = _loop(loop_id="loop-a")

    store.create_continuation_loop(workspace=tmp_path, loop=loop)

    loaded = store.load_continuation_loop(workspace=tmp_path, loop_id="loop-a")
    listed = store.list_continuation_loops(workspace=tmp_path)

    assert loaded.loop.id == "loop-a"
    assert loaded.prompt == "finish the migration"
    assert loaded.status == "active"
    assert loaded.max_iterations == 3
    assert loaded.intensive is True
    assert loaded.verification_status == "pending"
    assert loaded.verification_promise == "VERIFIED"
    assert loaded.created_at == 1
    assert loaded.updated_at == 1
    assert len(listed) == 1
    assert listed[0].loop.id == "loop-a"
    assert listed[0].iteration == 0
    assert listed[0].verification_status == "pending"


def test_non_intensive_loop_rejects_verification_status(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    loop = ContinuationLoopState(
        loop=ContinuationLoopRef(id="loop-invalid"),
        prompt="finish the migration",
        intensive=False,
        verification_status="pending",
    )

    with pytest.raises(ValueError, match="must be not_required"):
        store.create_continuation_loop(workspace=tmp_path, loop=loop)


def test_continuation_loop_records_iterations_and_exhausts(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_continuation_loop(
        workspace=tmp_path,
        loop=_loop(loop_id="loop-iter", intensive=False),
    )

    first = store.record_continuation_loop_iteration(workspace=tmp_path, loop_id="loop-iter")
    second = store.record_continuation_loop_iteration(workspace=tmp_path, loop_id="loop-iter")
    third = store.record_continuation_loop_iteration(workspace=tmp_path, loop_id="loop-iter")
    fourth = store.record_continuation_loop_iteration(workspace=tmp_path, loop_id="loop-iter")

    assert first.status == "active"
    assert first.iteration == 1
    assert second.status == "active"
    assert second.iteration == 2
    assert third.status == "exhausted"
    assert third.iteration == 3
    assert third.finished_at == third.updated_at
    assert third.error == "continuation loop reached max iterations"
    assert fourth == third


def test_intensive_loop_reaches_verification_pending_instead_of_exhausted(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore()
    store.create_continuation_loop(workspace=tmp_path, loop=_loop(loop_id="loop-verify"))

    first = store.record_continuation_loop_iteration(workspace=tmp_path, loop_id="loop-verify")
    second = store.record_continuation_loop_iteration(workspace=tmp_path, loop_id="loop-verify")
    third = store.record_continuation_loop_iteration(workspace=tmp_path, loop_id="loop-verify")

    assert first.status == "active"
    assert second.status == "active"
    assert third.status == "active"
    assert third.iteration == 3
    assert third.verification_status == "pending"
    assert third.finished_at is None
    assert third.error == "intensive continuation loop reached verification pending state"


def test_intensive_loop_completion_requires_verification(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_continuation_loop(workspace=tmp_path, loop=_loop(loop_id="loop-complete"))

    pending = store.mark_continuation_loop_terminal(
        workspace=tmp_path,
        loop_id="loop-complete",
        status="completed",
    )
    completed = store.mark_continuation_loop_verified(
        workspace=tmp_path,
        loop_id="loop-complete",
    )

    assert pending.status == "active"
    assert pending.verification_status == "pending"
    assert pending.finished_at is None
    assert completed.status == "completed"
    assert completed.verification_status == "verified"
    assert completed.finished_at == completed.updated_at


def test_intensive_loop_verification_failure_keeps_loop_active(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_continuation_loop(workspace=tmp_path, loop=_loop(loop_id="loop-failed"))

    failed = store.mark_continuation_loop_verification_failed(
        workspace=tmp_path,
        loop_id="loop-failed",
        error="oracle found a regression",
    )

    assert failed.status == "active"
    assert failed.verification_status == "failed"
    assert failed.error == "oracle found a regression"


def test_continuation_loop_cancel_is_terminal_and_idempotent(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    store.create_continuation_loop(workspace=tmp_path, loop=_loop(loop_id="loop-cancel"))

    cancelled = store.cancel_continuation_loop(workspace=tmp_path, loop_id="loop-cancel")
    repeated = store.cancel_continuation_loop(workspace=tmp_path, loop_id="loop-cancel")

    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested_at == cancelled.updated_at
    assert cancelled.finished_at == cancelled.updated_at
    assert cancelled.error == "cancelled by user"
    assert repeated == cancelled


def test_continuation_loop_runtime_restart_loads_persisted_state(tmp_path: Path) -> None:
    database_path = tmp_path / "sessions.sqlite3"
    first_store = SqliteSessionStore(database_path=database_path)
    first_store.create_continuation_loop(workspace=tmp_path, loop=_loop(loop_id="loop-restart"))
    _ = first_store.record_continuation_loop_iteration(
        workspace=tmp_path,
        loop_id="loop-restart",
    )

    second_store = SqliteSessionStore(database_path=database_path)
    loaded = second_store.load_continuation_loop(workspace=tmp_path, loop_id="loop-restart")

    assert loaded.loop.id == "loop-restart"
    assert loaded.iteration == 1
    assert loaded.status == "active"
    assert loaded.verification_status == "pending"
