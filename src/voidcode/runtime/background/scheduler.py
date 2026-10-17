from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts import RuntimeRequest
from .models import BackgroundTaskState
from .schedule_expr import next_fire_after, parse_schedule

if TYPE_CHECKING:
    from ..storage.ports import ScheduledTaskRepository
    from .supervisor import RuntimeBackgroundTaskSupervisor

logger = logging.getLogger(__name__)

# Claude parity: 5s tick loop for schedule evaluation
SCHEDULER_TICK_SECONDS: float = 5.0

# Claude parity: 7d default expiration for recurring schedules
DEFAULT_RECURRING_EXPIRY_SECONDS: int = 7 * 86400


class SchedulerThread:
    """Daemon thread executing periodic scheduled task evaluations.

    Lifecycle is owned by VoidCodeRuntime, starting alongside background
    services and stopping first on shutdown to drain in-flight cron firings.
    """

    def __init__(
        self,
        *,
        store: ScheduledTaskRepository,
        supervisor: RuntimeBackgroundTaskSupervisor | Any,
        workspace: Path,
        start_task_fn: Callable[[RuntimeRequest], BackgroundTaskState] | None = None,
        clock: Callable[[], float] = time.time,
        tick_interval_seconds: float = SCHEDULER_TICK_SECONDS,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._workspace = workspace
        self._start_task_fn = start_task_fn
        self._clock = clock
        self._tick_interval = tick_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the background daemon scheduler thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=f"SchedulerThread-{self._workspace.name}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float = 2.0) -> None:
        """Stop the background scheduler thread and wait for it to join."""
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_seconds)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Scheduler tick encountered unexpected error in workspace %s", self._workspace)
            if self._stop_event.wait(timeout=self._tick_interval):
                break

    def tick(self, now: int | None = None) -> list[str]:
        """Execute a single deterministic scheduler tick.

        1. Deletes expired schedules past expiry where final fire already occurred.
        2. Logs debug messages for overlapping schedules skipped because their task is running.
        3. Claims due schedules via atomic CAS update.
        4. Fires claimed tasks via supervisor / start_background_task and records running_task_id.
        """
        current_time = now if now is not None else int(self._clock())

        # 1. Expired recurring cleanup (past expiry at claim time)
        _ = self._store.delete_expired_scheduled_tasks(now=current_time, workspace=self._workspace)

        # 2. Overlap detection: find tasks that are due but already running (skip-if-running)
        all_schedules = self._store.list_scheduled_tasks(workspace=self._workspace)
        for sched in all_schedules:
            if sched["enabled"] and sched["next_fire_at"] <= current_time and sched["running_task_id"] is not None:
                logger.debug(
                    "Scheduled task %s skipped: background task %s still running",
                    sched["id"],
                    sched["running_task_id"],
                )

        # 3. Atomic claim CAS: running_task_id IS NULL AND next_fire_at <= now AND enabled
        claimed = self._store.claim_due_schedules(current_time, workspace=self._workspace)
        if not claimed:
            return []

        fired_task_ids: list[str] = []
        for sched in claimed:
            sched_id = sched["id"]
            request = RuntimeRequest(
                prompt=sched["prompt"],
                parent_session_id=sched["parent_session_id"],
                metadata={
                    "schedule_id": sched_id,
                    "scheduled_task_id": sched_id,
                },
            )
            try:
                if self._start_task_fn is not None:
                    state = self._start_task_fn(request)
                else:
                    state = self._supervisor.start_background_task(request)
                task_id = state.task.id
                self._store.record_schedule_running_task_id(schedule_id=sched_id, running_task_id=task_id)
                fired_task_ids.append(task_id)
            except Exception:
                logger.exception("Failed to fire background task for schedule %s", sched_id)
                # Release temporary claim token back to NULL on spawn failure
                self._store.release_scheduled_task(schedule_id=sched_id, next_fire_at=sched["next_fire_at"])

        return fired_task_ids

    def on_task_terminal(self, task: BackgroundTaskState) -> None:
        """Callback invoked when any background task enters a terminal state."""
        # Find schedule associated with this task
        sched_row = self._store.get_scheduled_task_by_running_task_id(running_task_id=task.task.id)
        if sched_row is None:
            schedule_id = task.request.metadata.get("schedule_id")
            if isinstance(schedule_id, str):
                sched_row = self._store.get_scheduled_task(schedule_id=schedule_id, workspace=self._workspace)
        if sched_row is None:
            return

        now = int(self._clock())
        sched_id = sched_row["id"]
        kind = sched_row["kind"]
        status = task.status

        if kind in ("at", "once"):
            # One-shot: delete on success; keep DISABLED for inspection on failure (OpenClaw parity)
            if status == "completed":
                self._store.delete_scheduled_task(schedule_id=sched_id, workspace=self._workspace)
            else:
                self._store.release_scheduled_task_failure(schedule_id=sched_id)
        else:
            # Recurring: check expiry
            expires_at = sched_row["expires_at"]
            if expires_at is not None and expires_at <= now:
                # Final fire already happened -> delete schedule
                self._store.delete_scheduled_task(schedule_id=sched_id, workspace=self._workspace)
            else:
                # Recompute next_fire_at from NOW (missed-fire catch-up: never replay past slots)
                expr = parse_schedule(sched_row["schedule_text"])
                next_fire = next_fire_after(expr, now)
                self._store.release_scheduled_task(schedule_id=sched_id, next_fire_at=next_fire)
