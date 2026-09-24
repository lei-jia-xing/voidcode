from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .rows import BackgroundProcessRow, decode_row, fetch_row, fetch_rows

if TYPE_CHECKING:
    from .shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _BackgroundProcessStorageMixin(_MixinBase):
    """Durable identity records for runtime-owned background processes."""

    def register_background_process(
        self,
        *,
        workspace: Path,
        process_id: str,
        owner_session_id: str | None,
        command: str,
        cwd: str,
        pid: int,
        process_group_id: int | None,
        process_identity: str | None,
        stdout_path: str,
        stderr_path: str,
    ) -> None:
        with self._write_connect(workspace) as connection:
            timestamp = self._next_auxiliary_timestamp(connection=connection)
            _ = connection.execute(
                """
                INSERT INTO background_processes (
                    process_id, workspace_id, owner_session_id, command, cwd, pid,
                    process_group_id, process_identity, stdout_path, stderr_path,
                    status, exit_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, ?, ?)
                """,
                (
                    process_id,
                    str(workspace),
                    owner_session_id,
                    command,
                    cwd,
                    pid,
                    process_group_id,
                    process_identity,
                    stdout_path,
                    stderr_path,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()

    def load_background_process(self, *, workspace: Path, process_id: str) -> dict[str, object] | None:
        with self._connect(workspace) as connection:
            row = fetch_row(
                connection,
                """
                    SELECT process_id, workspace_id, owner_session_id, command, cwd, pid,
                           process_group_id, process_identity, stdout_path, stderr_path,
                           status, exit_code, reconciliation_reason, created_at, updated_at
                    FROM background_processes
                    WHERE workspace_id = ? AND process_id = ?
                    """,
                (str(workspace), process_id),
            )
        return None if row is None else self._background_process_from_row(decode_row(row, BackgroundProcessRow))

    def list_background_processes(self, *, workspace: Path) -> tuple[dict[str, object], ...]:
        with self._connect(workspace) as connection:
            rows = fetch_rows(
                connection,
                """
                    SELECT process_id, workspace_id, owner_session_id, command, cwd, pid,
                           process_group_id, process_identity, stdout_path, stderr_path,
                           status, exit_code, reconciliation_reason, created_at, updated_at
                    FROM background_processes
                    WHERE workspace_id = ?
                    ORDER BY created_at ASC, process_id ASC
                    """,
                (str(workspace),),
            )
        return tuple(self._background_process_from_row(decode_row(row, BackgroundProcessRow)) for row in rows)

    def mark_background_process_exit(
        self,
        *,
        workspace: Path,
        process_id: str,
        status: str,
        exit_code: int | None,
        reconciliation_reason: str | None = None,
    ) -> None:
        with self._write_connect(workspace) as connection:
            timestamp = self._next_auxiliary_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_processes
                SET status = ?, exit_code = ?, reconciliation_reason = ?, updated_at = ?
                WHERE workspace_id = ? AND process_id = ?
                """,
                (status, exit_code, reconciliation_reason, timestamp, str(workspace), process_id),
            )
            connection.commit()

    @staticmethod
    def _background_process_from_row(row: BackgroundProcessRow) -> dict[str, object]:
        return {
            "process_id": row["process_id"],
            "workspace_id": row["workspace_id"],
            "owner_session_id": row["owner_session_id"],
            "command": row["command"],
            "cwd": row["cwd"],
            "pid": row["pid"],
            "process_group_id": row["process_group_id"],
            "process_identity": row["process_identity"],
            "stdout_path": row["stdout_path"],
            "stderr_path": row["stderr_path"],
            "status": row["status"],
            "exit_code": row["exit_code"],
            "reconciliation_reason": row["reconciliation_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


__all__ = ["_BackgroundProcessStorageMixin"]
