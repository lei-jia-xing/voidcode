from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

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
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT process_id, workspace_id, owner_session_id, command, cwd, pid,
                           process_group_id, process_identity, stdout_path, stderr_path,
                           status, exit_code, reconciliation_reason, created_at, updated_at
                    FROM background_processes
                    WHERE workspace_id = ? AND process_id = ?
                    """,
                    (str(workspace), process_id),
                ).fetchone(),
            )
        return None if row is None else self._background_process_from_row(row)

    def list_background_processes(self, *, workspace: Path) -> tuple[dict[str, object], ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT process_id, workspace_id, owner_session_id, command, cwd, pid,
                           process_group_id, process_identity, stdout_path, stderr_path,
                           status, exit_code, reconciliation_reason, created_at, updated_at
                    FROM background_processes
                    WHERE workspace_id = ?
                    ORDER BY created_at ASC, process_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._background_process_from_row(row) for row in rows)

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
    def _background_process_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "process_id": cast(str, row["process_id"]),
            "workspace_id": cast(str, row["workspace_id"]),
            "owner_session_id": cast(str | None, row["owner_session_id"]),
            "command": cast(str, row["command"]),
            "cwd": cast(str, row["cwd"]),
            "pid": cast(int, row["pid"]),
            "process_group_id": cast(int | None, row["process_group_id"]),
            "process_identity": cast(str | None, row["process_identity"]),
            "stdout_path": cast(str, row["stdout_path"]),
            "stderr_path": cast(str, row["stderr_path"]),
            "status": cast(str, row["status"]),
            "exit_code": cast(int | None, row["exit_code"]),
            "reconciliation_reason": cast(str | None, row["reconciliation_reason"]),
            "created_at": cast(int, row["created_at"]),
            "updated_at": cast(int, row["updated_at"]),
        }


__all__ = ["_BackgroundProcessStorageMixin"]
