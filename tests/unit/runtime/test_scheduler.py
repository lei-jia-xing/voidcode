from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    ScheduleSummary,
)
from voidcode.runtime.background.scheduler import SchedulerThread
from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.time = start

    def __call__(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


def _make_task_state(task_id: str, status: str = "running", schedule_id: str | None = None) -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        status=status,
        request=BackgroundTaskRequestSnapshot(
            prompt="test task",
            metadata={"schedule_id": schedule_id} if schedule_id else {},
        ),
        created_at=1000,
        updated_at=1000,
    )


def test_tick_fire_release_next_cycle(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "sched.db")
    clock = FakeClock(1000.0)

    # Mock supervisor
    mock_supervisor = MagicMock()
    mock_supervisor.start_background_task.return_value = _make_task_state("task-bg-1", "running")

    scheduler = SchedulerThread(
        store=store,
        supervisor=mock_supervisor,
        workspace=tmp_path,
        clock=clock,
        tick_interval_seconds=5.0,
    )

    # Create recurring schedule every 10m (600s), next_fire_at = 1000
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-recur",
        schedule_text="every 10m",
        kind="every",
        prompt="do recurring work",
        next_fire_at=1000,
        interval_seconds=600,
        created_at=500,
    )

    # 1. Tick at t=1000: claims and fires
    fired = scheduler.tick()
    assert fired == ["task-bg-1"]
    mock_supervisor.start_background_task.assert_called_once()
    req = mock_supervisor.start_background_task.call_args[0][0]
    assert req.prompt == "do recurring work"
    assert req.metadata["schedule_id"] == "sched-recur"

    # Task is now running
    row_running = store.get_scheduled_task(schedule_id="sched-recur", workspace=tmp_path)
    assert row_running is not None
    assert row_running["running_task_id"] == "task-bg-1"

    # 2. Complete task via terminal callback
    clock.advance(10.0)  # t=1010
    terminal_task = _make_task_state("task-bg-1", "completed", "sched-recur")
    scheduler.on_task_terminal(terminal_task)

    # Schedule should be released, next_fire_at recomputed to 1010 + 600 = 1610
    row_released = store.get_scheduled_task(schedule_id="sched-recur", workspace=tmp_path)
    assert row_released is not None
    assert row_released["running_task_id"] is None
    assert row_released["next_fire_at"] == 1610

    # 3. Next tick at t=1010 does NOT fire (not due yet)
    fired_early = scheduler.tick()
    assert fired_early == []

    # 4. Advance clock to t=1610 -> fires next cycle!
    clock.advance(600.0)  # t=1610
    mock_supervisor.start_background_task.return_value = _make_task_state("task-bg-2", "running")
    fired_cycle2 = scheduler.tick()
    assert fired_cycle2 == ["task-bg-2"]


def test_one_shot_success_delete_and_failure_disable(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "oneshot.db")
    clock = FakeClock(1000.0)
    mock_supervisor = MagicMock()

    scheduler = SchedulerThread(
        store=store,
        supervisor=mock_supervisor,
        workspace=tmp_path,
        clock=clock,
    )

    # 1. Success case: once schedule -> deletes on success
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-once-success",
        schedule_text="once",
        kind="once",
        prompt="one shot success",
        next_fire_at=1000,
        created_at=1000,
    )
    mock_supervisor.start_background_task.return_value = _make_task_state("task-success", "running")
    scheduler.tick()

    # Terminal success
    task_success = _make_task_state("task-success", "completed", "sched-once-success")
    scheduler.on_task_terminal(task_success)

    # Should be deleted!
    assert store.get_scheduled_task(schedule_id="sched-once-success", workspace=tmp_path) is None

    # 2. Failure case: at schedule -> kept disabled for inspection
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-at-fail",
        schedule_text="at 2026-01-01T12:00:00",
        kind="at",
        prompt="one shot fail",
        next_fire_at=1000,
        at_epoch=1000,
        created_at=1000,
    )
    mock_supervisor.start_background_task.return_value = _make_task_state("task-fail", "running")
    scheduler.tick()

    # Terminal failure
    task_failed = _make_task_state("task-fail", "failed", "sched-at-fail")
    scheduler.on_task_terminal(task_failed)

    # Should NOT be deleted, but disabled (enabled=0) and running_task_id=None
    failed_row = store.get_scheduled_task(schedule_id="sched-at-fail", workspace=tmp_path)
    assert failed_row is not None
    assert failed_row["enabled"] == 0
    assert failed_row["running_task_id"] is None

    # Subsequent tick does not fire it
    assert scheduler.tick() == []


def test_recurring_expire_final_fire_then_delete(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "expire.db")
    clock = FakeClock(1000.0)
    mock_supervisor = MagicMock()
    mock_supervisor.start_background_task.return_value = _make_task_state("task-exp", "running")

    scheduler = SchedulerThread(
        store=store,
        supervisor=mock_supervisor,
        workspace=tmp_path,
        clock=clock,
    )

    # Schedule: every 10s, created at 1000, next_fire_at=1010, expires_at=1015
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-exp",
        schedule_text="every 10s",
        kind="every",
        prompt="final fire task",
        next_fire_at=1010,
        interval_seconds=10,
        created_at=1000,
        expires_at=1015,
    )

    # At t=1010: final fire happens (1010 < 1015)
    clock.advance(10.0)  # t=1010
    fired = scheduler.tick()
    assert fired == ["task-exp"]

    # At t=1016: task completes after expires_at
    clock.advance(6.0)  # t=1016
    terminal_task = _make_task_state("task-exp", "completed", "sched-exp")
    scheduler.on_task_terminal(terminal_task)

    # Expired recurring schedule must be deleted on terminal!
    assert store.get_scheduled_task(schedule_id="sched-exp", workspace=tmp_path) is None


def test_missed_fire_catch_up(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "missed.db")
    clock = FakeClock(1000.0)
    mock_supervisor = MagicMock()
    mock_supervisor.start_background_task.return_value = _make_task_state("task-catchup", "running")

    scheduler = SchedulerThread(
        store=store,
        supervisor=mock_supervisor,
        workspace=tmp_path,
        clock=clock,
    )

    # Schedule: every 1m (60s), initial next_fire_at = 1060
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-catchup",
        schedule_text="every 1m",
        kind="every",
        prompt="catch up task",
        next_fire_at=1060,
        interval_seconds=60,
        created_at=1000,
    )

    # Simulate 1 hour sleep/suspension: jump clock from 1000 to 4600
    clock.time = 4600.0

    # Tick: fires ONCE
    fired = scheduler.tick()
    assert fired == ["task-catchup"]

    # Terminal complete at 4605
    clock.time = 4605.0
    terminal_task = _make_task_state("task-catchup", "completed", "sched-catchup")
    scheduler.on_task_terminal(terminal_task)

    # Recomputes next_fire_at from NOW (4605 + 60 = 4665), never replaying missed 60 slots!
    row = store.get_scheduled_task(schedule_id="sched-catchup", workspace=tmp_path)
    assert row is not None
    assert row["next_fire_at"] == 4665
    assert row["running_task_id"] is None


def test_skip_if_running_overlap(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "overlap.db")
    clock = FakeClock(1000.0)
    mock_supervisor = MagicMock()
    mock_supervisor.start_background_task.return_value = _make_task_state("task-1", "running")

    scheduler = SchedulerThread(
        store=store,
        supervisor=mock_supervisor,
        workspace=tmp_path,
        clock=clock,
    )

    # Create due schedule
    store.create_scheduled_task(
        workspace=tmp_path,
        schedule_id="sched-overlap",
        schedule_text="every 10s",
        kind="every",
        prompt="overlap task",
        next_fire_at=1000,
        interval_seconds=10,
        created_at=990,
    )

    # First tick fires task-1
    fired = scheduler.tick()
    assert fired == ["task-1"]
    assert mock_supervisor.start_background_task.call_count == 1

    # While task-1 is still running, advance clock to t=1020 (when next run would be due)
    clock.advance(20.0)

    # Tick again: skip-if-running!
    fired_second = scheduler.tick()
    assert fired_second == []
    # Supervisor should NOT have been called a second time
    assert mock_supervisor.start_background_task.call_count == 1


def test_runtime_service_methods(tmp_path: Path) -> None:
    config = RuntimeConfig(approval_mode="yolo")
    with VoidCodeRuntime(workspace=tmp_path, config=config) as runtime:
        # Create schedule
        summary = runtime.create_schedule(
            schedule="every 10m",
            prompt="summarize news",
            parent_session_id="parent-session-1",
        )
        assert isinstance(summary, ScheduleSummary)
        assert summary.schedule_text == "every 10m"
        assert summary.human_schedule == "every 10m"
        assert summary.prompt == "summarize news"
        assert summary.parent_session_id == "parent-session-1"
        assert summary.enabled is True
        assert summary.running_task_id is None
        assert summary.expires_at is not None  # recurring defaults to +7d

        # Get schedule
        loaded = runtime.get_schedule(schedule_id=summary.id)
        assert loaded is not None
        assert loaded.id == summary.id
        assert loaded.human_schedule == "every 10m"

        # List schedules
        listed = runtime.list_schedules()
        assert len(listed) == 1
        assert listed[0].id == summary.id

        # List with filter
        filtered = runtime.list_schedules(parent_session_id="parent-session-1")
        assert len(filtered) == 1
        assert len(runtime.list_schedules(parent_session_id="non-existent")) == 0

        # Delete schedule
        assert runtime.delete_schedule(schedule_id=summary.id) is True
        assert runtime.get_schedule(schedule_id=summary.id) is None
        assert len(runtime.list_schedules()) == 0
