from __future__ import annotations

import concurrent.futures
from pathlib import Path

from voidcode.runtime.storage import SqliteSessionStore


def test_scheduled_task_storage_crud(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "test.db")

    row = store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-1",
        schedule_text="every 10m",
        kind="every",
        prompt="hello world",
        next_fire_at=1000,
        interval_seconds=600,
        parent_session_id="parent-s1",
        enabled=True,
        created_at=500,
        expires_at=5000,
    )
    assert row["id"] == "sched-1"
    assert row["workspace"] == str(tmp_path)
    assert row["schedule_text"] == "every 10m"
    assert row["kind"] == "every"
    assert row["prompt"] == "hello world"
    assert row["next_fire_at"] == 1000
    assert row["interval_seconds"] == 600
    assert row["parent_session_id"] == "parent-s1"
    assert row["enabled"] == 1
    assert row["running_task_id"] is None
    assert row["created_at"] == 500
    assert row["expires_at"] == 5000

    # Get by ID
    loaded = store.get_scheduled_task(schedule_id="sched-1", workspace=tmp_path)
    assert loaded is not None
    assert loaded["id"] == "sched-1"

    # List
    listed = store.list_scheduled_tasks(workspace=tmp_path)
    assert len(listed) == 1
    assert listed[0]["id"] == "sched-1"

    # Filter list by parent_session_id
    listed_filtered = store.list_scheduled_tasks(workspace=tmp_path, parent_session_id="parent-s1")
    assert len(listed_filtered) == 1
    assert len(store.list_scheduled_tasks(workspace=tmp_path, parent_session_id="other")) == 0

    # Record running task ID
    assert store.record_schedule_running_task_id(schedule_id="sched-1", running_task_id="task-xyz")
    by_running = store.get_scheduled_task_by_running_task_id(running_task_id="task-xyz")
    assert by_running is not None
    assert by_running["id"] == "sched-1"

    # Release
    assert store.release_scheduled_task(schedule_id="sched-1", next_fire_at=1600)
    reloaded = store.get_scheduled_task(schedule_id="sched-1", workspace=tmp_path)
    assert reloaded is not None
    assert reloaded["running_task_id"] is None
    assert reloaded["next_fire_at"] == 1600
    assert reloaded["enabled"] == 1

    # Release failure (disables)
    assert store.release_scheduled_task_failure(schedule_id="sched-1")
    disabled = store.get_scheduled_task(schedule_id="sched-1", workspace=tmp_path)
    assert disabled is not None
    assert disabled["enabled"] == 0
    assert disabled["running_task_id"] is None

    # Delete
    assert store.delete_scheduled_task(schedule_id="sched-1", workspace=tmp_path)
    assert store.get_scheduled_task(schedule_id="sched-1", workspace=tmp_path) is None


def test_storage_claim_cas_two_concurrent_claimants_exactly_one_wins(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "cas.db")
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-race",
        schedule_text="once",
        kind="once",
        prompt="race task",
        next_fire_at=100,
        created_at=50,
    )

    results: list[tuple] = []

    def claim(token: str) -> None:
        # Each thread uses the store to claim due schedules
        claimed = store.claim_due_schedules(now=100, workspace=tmp_path, claim_token=token)
        results.append(claimed)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(claim, "claimant-1")
        f2 = executor.submit(claim, "claimant-2")
        f1.result()
        f2.result()

    # Exactly one claimant claimed the task, the other got empty tuple
    counts = [len(r) for r in results]
    assert sorted(counts) == [0, 1]


def test_storage_claim_cas_excludes_disabled_running_and_expired(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "filter.db")

    # 1. Normal due schedule -> should be claimed
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-due",
        schedule_text="once",
        kind="once",
        prompt="due task",
        next_fire_at=100,
        enabled=True,
        created_at=50,
    )

    # 2. Disabled schedule -> excluded
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-disabled",
        schedule_text="once",
        kind="once",
        prompt="disabled task",
        next_fire_at=100,
        enabled=False,
        created_at=50,
    )

    # 3. Already running schedule -> excluded
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-running",
        schedule_text="once",
        kind="once",
        prompt="running task",
        next_fire_at=100,
        enabled=True,
        created_at=50,
    )
    store.record_schedule_running_task_id(schedule_id="sched-running", running_task_id="task-already-running")

    # 4. Expired schedule (expires_at <= now) -> excluded
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-expired",
        schedule_text="every 10m",
        kind="every",
        prompt="expired task",
        next_fire_at=100,
        enabled=True,
        created_at=50,
        expires_at=90,  # 90 <= 100 -> expired
    )

    # 5. Future schedule (next_fire_at > now) -> excluded
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-future",
        schedule_text="once",
        kind="once",
        prompt="future task",
        next_fire_at=200,  # 200 > 100
        enabled=True,
        created_at=50,
    )

    # Claim at now = 100
    claimed = store.claim_due_schedules(now=100, workspace=tmp_path)
    claimed_ids = [r["id"] for r in claimed]

    assert claimed_ids == ["sched-due"]
