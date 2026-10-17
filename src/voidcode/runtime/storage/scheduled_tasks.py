from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .rows import ScheduledTaskRow, decode_row, fetch_row, fetch_rows

if TYPE_CHECKING:
    from .shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _ScheduledTaskStorageMixin(_MixinBase):
    """Persistence and atomic claim operations for scheduled tasks."""

    def create_scheduled_task(
        self,
        *,
        workspace: Path,
        schedule_id: str,
        schedule_text: str,
        kind: str,
        prompt: str,
        next_fire_at: int,
        interval_seconds: int | None = None,
        at_epoch: int | None = None,
        cron_fields: str | None = None,
        parent_session_id: str | None = None,
        enabled: bool = True,
        created_at: int | None = None,
        expires_at: int | None = None,
    ) -> ScheduledTaskRow:
        created = created_at if created_at is not None else int(time.time())
        with self._write_connect(workspace) as connection:
            row = connection.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, workspace, schedule_text, kind, interval_seconds, at_epoch, cron_fields,
                    prompt, parent_session_id, enabled, next_fire_at, running_task_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                RETURNING *
                """,
                (
                    schedule_id,
                    str(workspace),
                    schedule_text,
                    kind,
                    interval_seconds,
                    at_epoch,
                    cron_fields,
                    prompt,
                    parent_session_id,
                    1 if enabled else 0,
                    next_fire_at,
                    created,
                    expires_at,
                ),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("Failed to insert scheduled task")
            return decode_row(row, ScheduledTaskRow)

    def get_scheduled_task(self, *, schedule_id: str, workspace: Path | None = None) -> ScheduledTaskRow | None:
        target_workspace = workspace if workspace is not None else Path(".")
        with self._connect(target_workspace) as connection:
            if workspace is not None:
                sql = "SELECT * FROM scheduled_tasks WHERE id = ? AND workspace = ?"
                row = fetch_row(connection, sql, (schedule_id, str(workspace)))
            else:
                sql = "SELECT * FROM scheduled_tasks WHERE id = ?"
                row = fetch_row(connection, sql, (schedule_id,))
            return decode_row(row, ScheduledTaskRow) if row is not None else None

    def get_scheduled_task_by_running_task_id(self, *, running_task_id: str) -> ScheduledTaskRow | None:
        with self._connect(Path(".")) as connection:
            row = fetch_row(connection, "SELECT * FROM scheduled_tasks WHERE running_task_id = ?", (running_task_id,))
            return decode_row(row, ScheduledTaskRow) if row is not None else None

    def list_scheduled_tasks(
        self,
        *,
        workspace: Path | None = None,
        parent_session_id: str | None = None,
    ) -> tuple[ScheduledTaskRow, ...]:
        target_workspace = workspace if workspace is not None else Path(".")
        with self._connect(target_workspace) as connection:
            clauses = ["1=1"]
            params: list[object] = []
            if workspace is not None:
                clauses.append("workspace = ?")
                params.append(str(workspace))
            if parent_session_id is not None:
                clauses.append("parent_session_id = ?")
                params.append(parent_session_id)
            sql = f"SELECT * FROM scheduled_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at ASC"
            rows = fetch_rows(connection, sql, tuple(params))
            return tuple(decode_row(row, ScheduledTaskRow) for row in rows)

    def delete_scheduled_task(self, *, schedule_id: str, workspace: Path | None = None) -> bool:
        target_workspace = workspace if workspace is not None else Path(".")
        with self._write_connect(target_workspace) as connection:
            if workspace is not None:
                cursor = connection.execute(
                    "DELETE FROM scheduled_tasks WHERE id = ? AND workspace = ?",
                    (schedule_id, str(workspace)),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM scheduled_tasks WHERE id = ?",
                    (schedule_id,),
                )
            connection.commit()
            return cursor.rowcount > 0

    def claim_due_schedules(
        self,
        now: int,
        workspace: Path | None = None,
        *,
        claim_token: str | None = None,
    ) -> tuple[ScheduledTaskRow, ...]:
        """Atomically claim schedules due at or before ``now``.

        A single atomic CAS UPDATE ensures that if multiple callers race to
        claim due schedules, exactly one claimant wins each row.
        """
        claim_id = claim_token or f"claim-{uuid4().hex}"
        target_workspace = workspace if workspace is not None else Path(".")
        with self._write_connect(target_workspace) as connection:
            if workspace is not None:
                sql = """
                UPDATE scheduled_tasks
                SET running_task_id = ?
                WHERE next_fire_at <= ?
                  AND enabled = 1
                  AND running_task_id IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND workspace = ?
                RETURNING *
                """
                rows = fetch_rows(connection, sql, (claim_id, now, now, str(workspace)))
            else:
                sql = """
                UPDATE scheduled_tasks
                SET running_task_id = ?
                WHERE next_fire_at <= ?
                  AND enabled = 1
                  AND running_task_id IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                RETURNING *
                """
                rows = fetch_rows(connection, sql, (claim_id, now, now))
            connection.commit()
            return tuple(decode_row(row, ScheduledTaskRow) for row in rows)

    def record_schedule_running_task_id(self, *, schedule_id: str, running_task_id: str) -> bool:
        with self._write_connect(Path(".")) as connection:
            cursor = connection.execute(
                "UPDATE scheduled_tasks SET running_task_id = ? WHERE id = ?",
                (running_task_id, schedule_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def release_scheduled_task(self, *, schedule_id: str, next_fire_at: int) -> bool:
        with self._write_connect(Path(".")) as connection:
            cursor = connection.execute(
                "UPDATE scheduled_tasks SET running_task_id = NULL, next_fire_at = ? WHERE id = ?",
                (next_fire_at, schedule_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def release_scheduled_task_failure(self, *, schedule_id: str) -> bool:
        """Disable a failed one-shot schedule for inspection without auto-replay."""
        with self._write_connect(Path(".")) as connection:
            cursor = connection.execute(
                "UPDATE scheduled_tasks SET running_task_id = NULL, enabled = 0 WHERE id = ?",
                (schedule_id,),
            )
            connection.commit()
            return cursor.rowcount > 0

    def delete_expired_scheduled_tasks(self, *, now: int, workspace: Path | None = None) -> int:
        target_workspace = workspace if workspace is not None else Path(".")
        with self._write_connect(target_workspace) as connection:
            if workspace is not None:
                cursor = connection.execute(
                    "DELETE FROM scheduled_tasks WHERE expires_at IS NOT NULL AND expires_at <= ? AND workspace = ?",
                    (now, str(workspace)),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM scheduled_tasks WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now,),
                )
            connection.commit()
            return cursor.rowcount
