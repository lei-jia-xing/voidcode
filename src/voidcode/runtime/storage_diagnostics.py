from __future__ import annotations

import sqlite3
from pathlib import Path
from time import sleep, time
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _DiagnosticsStorageMixin(_MixinBase):
    def storage_diagnostics(self, *, workspace: Path) -> dict[str, object]:
        database_path = self._resolve_database_path()
        with self._connect(workspace) as connection:
            journal_mode = self._pragma_scalar(connection=connection, name="journal_mode")
            synchronous = self._pragma_scalar(connection=connection, name="synchronous")
            busy_timeout = self._pragma_scalar(connection=connection, name="busy_timeout")
            foreign_keys = self._pragma_scalar(connection=connection, name="foreign_keys")
            wal_autocheckpoint = self._pragma_scalar(
                connection=connection,
                name="wal_autocheckpoint",
            )
            checkpoint = self._wal_checkpoint(connection=connection, mode="PASSIVE")
            counts = self._storage_table_counts(connection=connection, workspace=workspace)
            task_status_counts = self._background_task_status_counts(
                connection=connection,
                workspace=workspace,
            )
            pending_counts = self._pending_state_counts(
                connection=connection,
                workspace=workspace,
            )
        return {
            "database_path": str(database_path),
            "database_exists": database_path.exists(),
            "sqlite_version": sqlite3.sqlite_version,
            "connection_policy": {
                "journal_mode": journal_mode,
                "synchronous": synchronous,
                "busy_timeout_ms": busy_timeout,
                "foreign_keys": foreign_keys,
                "wal_autocheckpoint_pages": wal_autocheckpoint,
            },
            "checkpoint": checkpoint,
            "file_sizes": self._database_file_sizes(database_path),
            "counts": counts,
            "background_task_status_counts": task_status_counts,
            "pending_counts": pending_counts,
        }

    def prune_runtime_storage(
        self,
        *,
        workspace: Path,
        keep_sessions: int | None = None,
        keep_background_tasks: int | None = None,
        older_than: int | None = None,
    ) -> dict[str, int]:
        if keep_sessions is not None and keep_sessions < 0:
            raise ValueError("keep_sessions must be non-negative when provided")
        if keep_background_tasks is not None and keep_background_tasks < 0:
            raise ValueError("keep_background_tasks must be non-negative when provided")
        if older_than is not None and older_than < 0:
            raise ValueError("older_than must be non-negative when provided")
        with self._write_connect(workspace) as connection:
            task_ids = self._prunable_background_task_ids(
                connection=connection,
                workspace=workspace,
                keep_background_tasks=keep_background_tasks,
                older_than=older_than,
            )
            retained_background_task_session_ids = self._retained_background_task_session_ids(
                connection=connection,
                workspace=workspace,
                pruned_task_ids=task_ids,
            )
            session_ids = self._prunable_session_ids(
                connection=connection,
                workspace=workspace,
                keep_sessions=keep_sessions,
                older_than=older_than,
                protected_session_ids=retained_background_task_session_ids,
            )
            counts = {
                "session_events": self._delete_for_ids(
                    connection=connection,
                    table="session_events",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "session_todos": self._delete_for_ids(
                    connection=connection,
                    table="session_todos",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "session_event_deliveries": self._delete_for_ids(
                    connection=connection,
                    table="session_event_deliveries",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "session_notifications": self._delete_for_ids(
                    connection=connection,
                    table="session_notifications",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "sessions": self._delete_for_ids(
                    connection=connection,
                    table="sessions",
                    column="session_id",
                    ids=session_ids,
                    workspace=workspace,
                ),
                "background_tasks": self._delete_for_ids(
                    connection=connection,
                    table="background_tasks",
                    column="task_id",
                    ids=task_ids,
                    workspace=workspace,
                ),
            }
            connection.commit()
            _ = self._wal_checkpoint(connection=connection, mode="PASSIVE")
        return counts

    def reset_runtime_storage(self, *, workspace: Path) -> dict[str, object]:
        _ = workspace
        database_path = self._resolve_database_path()
        removed: list[str] = []
        for path in (
            database_path,
            database_path.with_name(f"{database_path.name}-wal"),
            database_path.with_name(f"{database_path.name}-shm"),
        ):
            if path.exists():
                self._unlink_with_retries(path)
                removed.append(str(path))
        return {
            "database_path": str(database_path),
            "removed": removed,
            "reset": bool(removed),
        }

    @staticmethod
    def _unlink_with_retries(path: Path, *, attempts: int = 5, delay_seconds: float = 0.05) -> None:
        for attempt in range(attempts):
            try:
                path.unlink()
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                sleep(delay_seconds)

    @staticmethod
    def _pragma_scalar(*, connection: sqlite3.Connection, name: str) -> object:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        return None if row is None else cast(object, row[0])

    @staticmethod
    def _wal_checkpoint(*, connection: sqlite3.Connection, mode: str) -> dict[str, int]:
        row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row is None:
            return {"busy": 0, "log_pages": 0, "checkpointed_pages": 0}
        return {
            "busy": int(row[0]),
            "log_pages": int(row[1]),
            "checkpointed_pages": int(row[2]),
        }

    @staticmethod
    def _database_file_sizes(database_path: Path) -> dict[str, int]:
        candidates = {
            "database": database_path,
            "wal": database_path.with_name(f"{database_path.name}-wal"),
            "shm": database_path.with_name(f"{database_path.name}-shm"),
        }
        return {name: path.stat().st_size if path.exists() else 0 for name, path in candidates.items()}

    def _storage_table_counts(self, *, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]:
        scoped_tables = ("sessions", "background_tasks", "session_notifications")
        counts = {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE workspace_id = ?",
                    (str(workspace),),
                ).fetchone()[0]
            )
            for table in scoped_tables
        }
        session_ids = tuple(
            cast(str, row["session_id"])
            for row in cast(
                list[sqlite3.Row],
                connection.execute(
                    "SELECT session_id FROM sessions WHERE workspace_id = ?",
                    (str(workspace),),
                ).fetchall(),
            )
        )
        counts["session_events"] = self._count_for_ids(
            connection=connection,
            table="session_events",
            column="session_id",
            ids=session_ids,
            workspace=workspace,
        )
        counts["session_todos"] = self._count_for_ids(
            connection=connection,
            table="session_todos",
            column="session_id",
            ids=session_ids,
            workspace=workspace,
        )
        counts["session_event_deliveries"] = self._count_for_ids(
            connection=connection,
            table="session_event_deliveries",
            column="session_id",
            ids=session_ids,
            workspace=workspace,
        )
        return counts

    @staticmethod
    def _pending_state_counts(*, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN pending_approval_json IS NOT NULL THEN 1 ELSE 0 END) AS approvals,
                SUM(CASE WHEN pending_question_json IS NOT NULL THEN 1 ELSE 0 END) AS questions
            FROM sessions
            WHERE workspace_id = ?
            """,
            (str(workspace),),
        ).fetchone()
        if row is None:
            return {"pending_approvals": 0, "pending_questions": 0}
        return {
            "pending_approvals": int(row[0] or 0),
            "pending_questions": int(row[1] or 0),
        }

    @staticmethod
    def _count_for_ids(
        *,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        ids: tuple[str, ...],
        workspace: Path | None = None,
    ) -> int:
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        workspace_clause = " AND workspace_id = ?" if workspace is not None else ""
        parameters: tuple[object, ...] = (*ids, str(workspace)) if workspace is not None else ids
        return int(
            connection.execute(
                (f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders}){workspace_clause}"),
                parameters,
            ).fetchone()[0]
        )

    @staticmethod
    def _delete_for_ids(
        *,
        connection: sqlite3.Connection,
        table: str,
        column: str,
        ids: tuple[str, ...],
        workspace: Path | None = None,
    ) -> int:
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        workspace_clause = " AND workspace_id = ?" if workspace is not None else ""
        parameters: tuple[object, ...] = (*ids, str(workspace)) if workspace is not None else ids
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders}){workspace_clause}",
            parameters,
        )
        return cursor.rowcount

    @staticmethod
    def _prunable_session_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        keep_sessions: int | None,
        older_than: int | None,
        protected_session_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        conditions = ["workspace_id = ?", "status IN ('completed', 'failed')"]
        parameters: list[object] = [str(workspace)]
        if older_than is not None:
            conditions.append("updated_at < ?")
            parameters.append(older_than)
        protected_clause = ""
        if protected_session_ids:
            protected_placeholders = ", ".join("?" for _ in protected_session_ids)
            protected_clause = f"AND session_id NOT IN ({protected_placeholders})"
            parameters.extend(protected_session_ids)
        keep_clause = ""
        if keep_sessions is not None:
            keep_clause = (
                "AND session_id NOT IN ("
                "SELECT session_id FROM sessions "
                "WHERE workspace_id = ? AND status IN ('completed', 'failed') "
                "ORDER BY updated_at DESC, session_id ASC LIMIT ?"
                ")"
            )
            parameters.extend([str(workspace), keep_sessions])
        rows = connection.execute(
            f"""
            SELECT session_id
            FROM sessions
            WHERE {" AND ".join(conditions)}
              {protected_clause}
              {keep_clause}
            ORDER BY updated_at ASC, session_id ASC
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["session_id"]) for row in cast(list[sqlite3.Row], rows))

    @staticmethod
    def _retained_background_task_session_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        pruned_task_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        pruned_clause = ""
        parameters: list[object] = [str(workspace)]
        if pruned_task_ids:
            pruned_placeholders = ", ".join("?" for _ in pruned_task_ids)
            pruned_clause = f"AND task_id NOT IN ({pruned_placeholders})"
            parameters.extend(pruned_task_ids)
        rows = connection.execute(
            f"""
            SELECT DISTINCT session_id
            FROM background_tasks
            WHERE workspace_id = ?
              AND session_id IS NOT NULL
              {pruned_clause}
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["session_id"]) for row in cast(list[sqlite3.Row], rows))

    @staticmethod
    def _prunable_background_task_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        keep_background_tasks: int | None,
        older_than: int | None,
    ) -> tuple[str, ...]:
        conditions = [
            "workspace_id = ?",
            "status IN ('completed', 'failed', 'cancelled', 'interrupted')",
        ]
        parameters: list[object] = [str(workspace)]
        if older_than is not None:
            conditions.append("updated_at < ?")
            parameters.append(older_than)
        keep_clause = ""
        if keep_background_tasks is not None:
            keep_clause = (
                "AND task_id NOT IN ("
                "SELECT task_id FROM background_tasks "
                "WHERE workspace_id = ? AND status IN "
                "('completed', 'failed', 'cancelled', 'interrupted') "
                "ORDER BY updated_at DESC, task_id ASC LIMIT ?"
                ")"
            )
            parameters.extend([str(workspace), keep_background_tasks])
        rows = connection.execute(
            f"""
            SELECT task_id
            FROM background_tasks
            WHERE {" AND ".join(conditions)}
              {keep_clause}
            ORDER BY updated_at ASC, task_id ASC
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["task_id"]) for row in cast(list[sqlite3.Row], rows))

    def _auto_prune_sessions(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
    ) -> int:
        age_cutoff_ms = int((time() - self._DEFAULT_MAX_SESSION_AGE_DAYS * 86_400) * 1000)
        # Child sessions referenced by a background task must survive count/age
        # pruning: ``load_background_task_child_result`` / parent-terminal event
        # backfill read them long after the task finished. Mirrors the explicit
        # ``prune_runtime_storage`` protection via ``_retained_background_task_session_ids``.
        protected_task_session_ids = self._retained_background_task_session_ids(
            connection=connection,
            workspace=workspace,
            pruned_task_ids=(),
        )
        age_rows = cast(
            list[sqlite3.Row],
            connection.execute(
                """
                SELECT session_id FROM sessions
                WHERE workspace_id = ?
                  AND status IN ('completed', 'failed')
                  AND created_at_unix_ms IS NOT NULL
                  AND created_at_unix_ms < ?
                ORDER BY created_at_unix_ms ASC, session_id ASC
                """,
                (str(workspace), age_cutoff_ms),
            ).fetchall(),
        )
        age_ids = tuple(
            session_id for session_id in (cast(str, row["session_id"]) for row in age_rows) if session_id not in protected_task_session_ids
        )

        count_ids = self._prunable_session_ids(
            connection=connection,
            workspace=workspace,
            keep_sessions=self._DEFAULT_MAX_SESSIONS_PER_WORKSPACE,
            older_than=None,
            protected_session_ids=protected_task_session_ids,
        )
        # Dangling-parent terminal children: a child session whose parent row is
        # gone (pruned, or a synthetic parent that was never persisted) and that
        # no background task references can never be reached again — prune them
        # so the ``child.parent_id must exist`` linkage invariant converges
        # instead of accumulating orphans forever.
        dangling_child_ids = self._dangling_parent_terminal_session_ids(
            connection=connection,
            workspace=workspace,
            protected_session_ids=protected_task_session_ids,
        )
        pruned_ids = tuple(dict.fromkeys((*count_ids, *age_ids, *dangling_child_ids)))
        # Orphaned terminal background tasks: tasks that reached a terminal
        # status without ever allocating a child session (e.g. shutdown
        # terminalization of still-queued tasks) and whose parent session is
        # terminal or missing are pure garbage — no result, no child session, no
        # repair path. Delete them here (the existing list-triggered prune path)
        # so they cannot accumulate unbounded across shutdowns.
        orphaned_task_ids = self._orphaned_terminal_background_task_ids(
            connection=connection,
            workspace=workspace,
        )
        if not pruned_ids and not orphaned_task_ids:
            return 0

        for table in (
            "session_events",
            "session_todos",
            "session_event_deliveries",
            "session_notifications",
            "sessions",
        ):
            self._delete_for_ids(
                connection=connection,
                table=table,
                column="session_id",
                ids=pruned_ids,
                workspace=workspace,
            )
        if orphaned_task_ids:
            self._delete_for_ids(
                connection=connection,
                table="background_tasks",
                column="task_id",
                ids=orphaned_task_ids,
                workspace=workspace,
            )
        return len(pruned_ids) + len(orphaned_task_ids)

    @staticmethod
    def _dangling_parent_terminal_session_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        protected_session_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Event-less terminal children whose parent row is gone and no task references.

        Every real run persists at least one event before a terminal seal
        (request_received / the terminal failure), so a terminal session with an
        EMPTY event log is pure fabrication residue (direct store writes, stale
        bundle rows) — it has no transcript, no result, and no repair path.
        Combined with a dangling parent and no task reference, it can never be
        reached again; prune it so the ``child.parent_id must exist`` linkage
        converges instead of accumulating orphans forever.
        """
        protected_clause = ""
        parameters: list[object] = [str(workspace)]
        if protected_session_ids:
            protected_placeholders = ", ".join("?" for _ in protected_session_ids)
            protected_clause = f"AND session_id NOT IN ({protected_placeholders})"
            parameters.extend(protected_session_ids)
        rows = connection.execute(
            f"""
            SELECT session_id
            FROM sessions
            WHERE workspace_id = ?
              AND status IN ('completed', 'failed')
              AND parent_session_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM session_events event
                  WHERE event.workspace_id = sessions.workspace_id
                    AND event.session_id = sessions.session_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM sessions parent
                  WHERE parent.workspace_id = sessions.workspace_id
                    AND parent.session_id = sessions.parent_session_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM background_tasks task
                  WHERE task.workspace_id = sessions.workspace_id
                    AND task.session_id = sessions.session_id
              )
              {protected_clause}
            ORDER BY updated_at ASC, session_id ASC
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(cast(str, row["session_id"]) for row in cast(list[sqlite3.Row], rows))

    @staticmethod
    def _orphaned_terminal_background_task_ids(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
    ) -> tuple[str, ...]:
        """Terminal tasks with no child session whose parent is terminal or gone.

        These arise from shutdown terminalization of still-queued tasks and from
        restart reconciliation of running tasks whose worker died before any
        child session persisted. They have no result, no child session, and no
        repair path (``repair_interrupted_task_from_child_terminal_session``
        requires ``session_id``), so they are safe to prune; a terminal parent
        (or missing parent) guarantees no future dispatch can own them.
        """
        rows = connection.execute(
            """
            SELECT task_id
            FROM background_tasks
            WHERE workspace_id = ?
              AND status IN ('completed', 'failed', 'cancelled', 'interrupted')
              AND session_id IS NULL
              AND (
                  request_parent_session_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM sessions parent
                      WHERE parent.workspace_id = background_tasks.workspace_id
                        AND parent.session_id = background_tasks.request_parent_session_id
                        AND parent.status NOT IN ('completed', 'failed')
                  )
              )
            ORDER BY updated_at ASC, task_id ASC
            """,
            (str(workspace),),
        ).fetchall()
        return tuple(cast(str, row["task_id"]) for row in cast(list[sqlite3.Row], rows))
