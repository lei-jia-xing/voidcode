from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, cast, final, runtime_checkable

from .contracts import RuntimeRequest, RuntimeResponse
from .events import EventEnvelope, EventSource
from .permission import PendingApproval
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
    validate_background_task_id,
)


@runtime_checkable
class SessionStore(Protocol):
    def save_run(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        clear_pending_approval: bool = True,
    ) -> None: ...

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]: ...

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse: ...

    def save_pending_approval(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> None: ...

    def load_pending_approval(
        self, *, workspace: Path, session_id: str
    ) -> PendingApproval | None: ...

    def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None: ...

    def load_resume_checkpoint(
        self, *, workspace: Path, session_id: str
    ) -> dict[str, object] | None: ...

    def create_background_task(
        self,
        *,
        workspace: Path,
        task: BackgroundTaskState,
    ) -> None: ...

    def load_background_task(self, *, workspace: Path, task_id: str) -> BackgroundTaskState: ...

    def list_background_tasks(
        self, *, workspace: Path
    ) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def mark_background_task_running(
        self,
        *,
        workspace: Path,
        task_id: str,
        session_id: str,
    ) -> BackgroundTaskState: ...

    def mark_background_task_terminal(
        self,
        *,
        workspace: Path,
        task_id: str,
        status: BackgroundTaskStatus,
        error: str | None = None,
    ) -> BackgroundTaskState: ...

    def request_background_task_cancel(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState: ...

    def fail_incomplete_background_tasks(
        self,
        *,
        workspace: Path,
        message: str,
    ) -> tuple[BackgroundTaskState, ...]: ...


@final
class SqliteSessionStore:
    _database_path: Path | None

    def __init__(self, *, database_path: Path | None = None) -> None:
        self._database_path = database_path

    def _resolve_database_path(self, workspace: Path) -> Path:
        if self._database_path is not None:
            return self._database_path
        return workspace / ".voidcode" / "sessions.sqlite3"

    @contextmanager
    def _connect(self, workspace: Path) -> Iterator[sqlite3.Connection]:
        database_path = self._resolve_database_path(workspace)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        try:
            connection.row_factory = sqlite3.Row
            self._ensure_schema(connection)
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                turn INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                output TEXT,
                metadata_json TEXT NOT NULL,
                pending_approval_json TEXT,
                resume_checkpoint_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_event_sequence INTEGER NOT NULL
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_events (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS background_tasks (
                task_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                prompt TEXT NOT NULL,
                request_session_id TEXT,
                request_metadata_json TEXT NOT NULL,
                allocate_session_id INTEGER NOT NULL,
                session_id TEXT,
                error TEXT,
                cancel_requested_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER
            )
            """
        )
        user_version = self._read_user_version(connection=connection)
        columns = {
            cast(str, row["name"])
            for row in cast(
                list[sqlite3.Row], connection.execute("PRAGMA table_info(sessions)").fetchall()
            )
        }
        target_user_version = user_version
        if "pending_approval_json" not in columns:
            _ = connection.execute("ALTER TABLE sessions ADD COLUMN pending_approval_json TEXT")
            target_user_version = max(target_user_version, 1)
        if "resume_checkpoint_json" not in columns:
            _ = connection.execute("ALTER TABLE sessions ADD COLUMN resume_checkpoint_json TEXT")
            target_user_version = max(target_user_version, 3)
        if user_version < 2:
            target_user_version = max(target_user_version, 2)
        if user_version < 3:
            target_user_version = max(target_user_version, 3)
        if user_version < 4:
            target_user_version = max(target_user_version, 4)
        if target_user_version != user_version:
            _ = connection.execute(f"PRAGMA user_version = {target_user_version}")
        connection.commit()

    @staticmethod
    def _parse_session_status(value: str) -> SessionStatus:
        allowed: tuple[SessionStatus, ...] = ("idle", "running", "waiting", "completed", "failed")
        if value not in allowed:
            raise ValueError(f"invalid session status: {value}")
        return value

    @staticmethod
    def _parse_event_source(value: str) -> EventSource:
        allowed: tuple[EventSource, ...] = ("runtime", "graph", "tool")
        if value not in allowed:
            raise ValueError(f"invalid event source: {value}")
        return value

    @staticmethod
    def _parse_background_task_status(value: str) -> BackgroundTaskStatus:
        allowed: tuple[BackgroundTaskStatus, ...] = (
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
        )
        if value not in allowed:
            raise ValueError(f"invalid background task status: {value}")
        return value

    def save_run(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        clear_pending_approval: bool = True,
    ) -> None:
        session_id = response.session.session.id
        with self._connect(workspace) as connection:
            created_at = self._read_created_at(connection=connection, session_id=session_id)
            updated_at = self._next_timestamp(connection=connection)
            _ = connection.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    session_id, workspace, status, turn, prompt, output,
                    metadata_json, pending_approval_json, resume_checkpoint_json, created_at, updated_at, last_event_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,  # noqa: E501
                (
                    session_id,
                    str(workspace),
                    response.session.status,
                    response.session.turn,
                    request.prompt,
                    response.output,
                    json.dumps(response.session.metadata, sort_keys=True),
                    None
                    if clear_pending_approval
                    else self._read_pending_approval_json(
                        connection=connection, session_id=session_id
                    ),
                    json.dumps(
                        self._terminal_resume_checkpoint(
                            request=request,
                            response=response,
                        ),
                        sort_keys=True,
                    ),
                    created_at,
                    updated_at,
                    response.events[-1].sequence if response.events else 0,
                ),
            )
            _ = connection.execute("DELETE FROM session_events WHERE session_id = ?", (session_id,))
            _ = connection.executemany(
                """
                INSERT INTO session_events (session_id, sequence, event_type, source, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.session_id,
                        event.sequence,
                        event.event_type,
                        event.source,
                        json.dumps(event.payload, sort_keys=True),
                    )
                    for event in response.events
                ],
            )
            connection.commit()

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                SELECT session_id, status, turn, prompt, updated_at
                FROM sessions
                WHERE workspace = ?
                ORDER BY updated_at DESC, session_id ASC
                """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(
            StoredSessionSummary(
                session=SessionRef(id=cast(str, row["session_id"])),
                status=self._parse_session_status(cast(str, row["status"])),
                turn=cast(int, row["turn"]),
                prompt=cast(str, row["prompt"]),
                updated_at=cast(int, row["updated_at"]),
            )
            for row in rows
        )

    def save_pending_approval(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> None:
        session_id = response.session.session.id
        with self._connect(workspace) as connection:
            created_at = self._read_created_at(connection=connection, session_id=session_id)
            updated_at = self._next_timestamp(connection=connection)
            _ = connection.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    session_id, workspace, status, turn, prompt, output,
                    metadata_json, pending_approval_json, resume_checkpoint_json, created_at, updated_at, last_event_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,  # noqa: E501
                (
                    session_id,
                    str(workspace),
                    response.session.status,
                    response.session.turn,
                    request.prompt,
                    response.output,
                    json.dumps(response.session.metadata, sort_keys=True),
                    json.dumps(asdict(pending_approval), sort_keys=True),
                    json.dumps(
                        self._approval_wait_resume_checkpoint(
                            request=request,
                            response=response,
                            pending_approval=pending_approval,
                        ),
                        sort_keys=True,
                    ),
                    created_at,
                    updated_at,
                    response.events[-1].sequence if response.events else 0,
                ),
            )
            _ = connection.execute("DELETE FROM session_events WHERE session_id = ?", (session_id,))
            _ = connection.executemany(
                """
                INSERT INTO session_events (session_id, sequence, event_type, source, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.session_id,
                        event.sequence,
                        event.event_type,
                        event.source,
                        json.dumps(event.payload, sort_keys=True),
                    )
                    for event in response.events
                ],
            )
            connection.commit()

    def load_pending_approval(self, *, workspace: Path, session_id: str) -> PendingApproval | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT pending_approval_json
                    FROM sessions
                    WHERE workspace = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise ValueError(f"unknown session: {session_id}")
        payload = cast(str | None, row["pending_approval_json"])
        if payload is None:
            return None
        data = cast(dict[str, object], json.loads(payload))
        raw_policy_mode = data.get("policy_mode", "ask")
        if raw_policy_mode not in ("allow", "deny", "ask"):
            raise ValueError(f"invalid permission policy mode: {raw_policy_mode}")
        return PendingApproval(
            request_id=cast(str, data["request_id"]),
            tool_name=cast(str, data["tool_name"]),
            arguments=cast(dict[str, object], data.get("arguments", {})),
            target_summary=cast(str, data.get("target_summary", "")),
            reason=cast(str, data.get("reason", "")),
            policy_mode=raw_policy_mode,
        )

    def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None:
        with self._connect(workspace) as connection:
            _ = connection.execute(
                "UPDATE sessions SET pending_approval_json = NULL WHERE workspace = ? AND session_id = ?",  # noqa: E501
                (str(workspace), session_id),
            )
            connection.commit()

    def load_resume_checkpoint(
        self, *, workspace: Path, session_id: str
    ) -> dict[str, object] | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT resume_checkpoint_json
                    FROM sessions
                    WHERE workspace = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise ValueError(f"unknown session: {session_id}")
        payload = cast(str | None, row["resume_checkpoint_json"])
        if payload is None:
            return None
        try:
            checkpoint = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(checkpoint, dict):
            return None
        return cast(dict[str, object], checkpoint)

    def _read_pending_approval_json(
        self, *, connection: sqlite3.Connection, session_id: str
    ) -> str | None:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT pending_approval_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone(),
        )
        if row is None:
            return None
        return cast(str | None, row["pending_approval_json"])

    @staticmethod
    def _approval_wait_resume_checkpoint(
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> dict[str, object]:
        return {
            "version": 1,
            "kind": "approval_wait",
            "prompt": request.prompt,
            "session_status": response.session.status,
            "session_metadata": response.session.metadata,
            "tool_results": SqliteSessionStore._tool_results_from_events(response.events),
            "last_event_sequence": response.events[-1].sequence if response.events else 0,
            "pending_approval_request_id": pending_approval.request_id,
            "output": response.output,
        }

    @staticmethod
    def _terminal_resume_checkpoint(
        *, request: RuntimeRequest, response: RuntimeResponse
    ) -> dict[str, object]:
        return {
            "version": 1,
            "kind": "terminal",
            "prompt": request.prompt,
            "session_status": response.session.status,
            "session_metadata": response.session.metadata,
            "tool_results": SqliteSessionStore._tool_results_from_events(response.events),
            "last_event_sequence": response.events[-1].sequence if response.events else 0,
            "output": response.output,
        }

    @staticmethod
    def _tool_results_from_events(events: tuple[EventEnvelope, ...]) -> list[dict[str, object]]:
        tool_results: list[dict[str, object]] = []
        for event in events:
            if event.event_type != "runtime.tool_completed":
                continue
            payload = event.payload
            is_err = "error" in payload
            tool_results.append(
                {
                    "tool_name": str(payload.get("tool", "unknown")),
                    "content": str(payload.get("content", "")) if not is_err else None,
                    "status": "error" if is_err else "ok",
                    "data": payload,
                    "error": str(payload["error"]) if is_err else None,
                }
            )
        return tool_results

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse:
        with self._connect(workspace) as connection:
            session_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                SELECT session_id, status, turn, output, metadata_json
                FROM sessions
                WHERE workspace = ? AND session_id = ?
                """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if session_row is None:
                raise ValueError(f"unknown session: {session_id}")
            event_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                SELECT sequence, event_type, source, payload_json
                FROM session_events
                WHERE session_id = ?
                ORDER BY sequence ASC
                """,
                    (session_id,),
                ).fetchall(),
            )
        session = SessionState(
            session=SessionRef(id=cast(str, session_row["session_id"])),
            status=self._parse_session_status(cast(str, session_row["status"])),
            turn=cast(int, session_row["turn"]),
            metadata=cast(dict[str, object], json.loads(cast(str, session_row["metadata_json"]))),
        )
        events = tuple(
            EventEnvelope(
                session_id=session_id,
                sequence=cast(int, row["sequence"]),
                event_type=cast(str, row["event_type"]),
                source=self._parse_event_source(cast(str, row["source"])),
                payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
            )
            for row in event_rows
        )
        return RuntimeResponse(
            session=session, events=events, output=cast(str | None, session_row["output"])
        )

    def create_background_task(
        self,
        *,
        workspace: Path,
        task: BackgroundTaskState,
    ) -> None:
        task_id = validate_background_task_id(task.task.id)
        with self._connect(workspace) as connection:
            _ = connection.execute(
                """
                INSERT INTO background_tasks (
                    task_id, workspace, status, prompt, request_session_id,
                    request_metadata_json, allocate_session_id, session_id, error,
                    cancel_requested_at, created_at, updated_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(workspace),
                    task.status,
                    task.request.prompt,
                    task.request.session_id,
                    json.dumps(task.request.metadata, sort_keys=True),
                    1 if task.request.allocate_session_id else 0,
                    task.session_id,
                    task.error,
                    task.cancel_requested_at,
                    task.created_at,
                    task.updated_at,
                    task.started_at,
                    task.finished_at,
                ),
            )
            connection.commit()

    def load_background_task(self, *, workspace: Path, task_id: str) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT * FROM background_tasks
                    WHERE workspace = ? AND task_id = ?
                    """,
                    (str(workspace), task_id),
                ).fetchone(),
            )
        if row is None:
            raise ValueError(f"unknown background task: {task_id}")
        return self._background_task_state_from_row(row)

    def list_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                    FROM background_tasks
                    WHERE workspace = ?
                    ORDER BY updated_at DESC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(
            StoredBackgroundTaskSummary(
                task=BackgroundTaskRef(id=cast(str, row["task_id"])),
                status=self._parse_background_task_status(cast(str, row["status"])),
                prompt=cast(str, row["prompt"]),
                session_id=cast(str | None, row["session_id"]),
                error=cast(str | None, row["error"]),
                created_at=cast(int, row["created_at"]),
                updated_at=cast(int, row["updated_at"]),
            )
            for row in rows
        )

    def mark_background_task_running(
        self,
        *,
        workspace: Path,
        task_id: str,
        session_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._connect(workspace) as connection:
            updated_at = self._next_background_task_timestamp(connection=connection)
            updated = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, session_id = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE workspace = ? AND task_id = ? AND status = 'queued'
                """,
                ("running", session_id, updated_at, updated_at, str(workspace), task_id),
            ).rowcount
            connection.commit()
        if updated == 0:
            return self.load_background_task(workspace=workspace, task_id=task_id)
        return self.load_background_task(workspace=workspace, task_id=task_id)

    def mark_background_task_terminal(
        self,
        *,
        workspace: Path,
        task_id: str,
        status: BackgroundTaskStatus,
        error: str | None = None,
    ) -> BackgroundTaskState:
        if status not in ("completed", "failed", "cancelled"):
            raise ValueError(
                "background task terminal status must be completed, failed, or cancelled"
            )
        task_id = validate_background_task_id(task_id)
        with self._connect(workspace) as connection:
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE workspace = ? AND task_id = ?
                """,
                (status, error, updated_at, updated_at, str(workspace), task_id),
            )
            connection.commit()
        return self.load_background_task(workspace=workspace, task_id=task_id)

    def request_background_task_cancel(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        current = self.load_background_task(workspace=workspace, task_id=task_id)
        if current.status == "queued":
            return self.mark_background_task_terminal(
                workspace=workspace,
                task_id=task_id,
                status="cancelled",
                error="cancelled before start",
            )
        if current.status in ("completed", "failed", "cancelled"):
            return current
        with self._connect(workspace) as connection:
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET cancel_requested_at = ?, updated_at = ?
                WHERE workspace = ? AND task_id = ?
                """,
                (updated_at, updated_at, str(workspace), task_id),
            )
            connection.commit()
        return self.load_background_task(workspace=workspace, task_id=task_id)

    def fail_incomplete_background_tasks(
        self,
        *,
        workspace: Path,
        message: str,
    ) -> tuple[BackgroundTaskState, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id FROM background_tasks
                    WHERE workspace = ? AND status IN ('queued', 'running')
                    ORDER BY updated_at ASC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
            if not rows:
                return ()
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = 'failed', error = ?, finished_at = ?, updated_at = ?
                WHERE workspace = ? AND status IN ('queued', 'running')
                """,
                (message, updated_at, updated_at, str(workspace)),
            )
            connection.commit()
        return tuple(
            self.load_background_task(workspace=workspace, task_id=cast(str, row["task_id"]))
            for row in rows
        )

    def _background_task_state_from_row(self, row: sqlite3.Row) -> BackgroundTaskState:
        metadata = json.loads(cast(str, row["request_metadata_json"]))
        if not isinstance(metadata, dict):
            raise ValueError("background task metadata must decode to an object")
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=cast(str, row["task_id"])),
            status=self._parse_background_task_status(cast(str, row["status"])),
            request=BackgroundTaskRequestSnapshot(
                prompt=cast(str, row["prompt"]),
                session_id=cast(str | None, row["request_session_id"]),
                metadata=cast(dict[str, object], metadata),
                allocate_session_id=bool(cast(int, row["allocate_session_id"])),
            ),
            session_id=cast(str | None, row["session_id"]),
            error=cast(str | None, row["error"]),
            created_at=cast(int, row["created_at"]),
            updated_at=cast(int, row["updated_at"]),
            started_at=cast(int | None, row["started_at"]),
            finished_at=cast(int | None, row["finished_at"]),
            cancel_requested_at=cast(int | None, row["cancel_requested_at"]),
        )

    def _next_background_task_timestamp(self, *, connection: sqlite3.Connection) -> int:
        row = cast(
            sqlite3.Row,
            connection.execute(
                "SELECT COALESCE(MAX(updated_at), 0) + 1 AS next_ts FROM background_tasks"
            ).fetchone(),
        )
        return cast(int, row["next_ts"])

    def _read_user_version(self, *, connection: sqlite3.Connection) -> int:
        row = cast(
            sqlite3.Row | tuple[object, ...] | None,
            connection.execute("PRAGMA user_version").fetchone(),
        )
        if row is None:
            return 0
        return cast(int, row[0])

    def _read_created_at(self, *, connection: sqlite3.Connection, session_id: str) -> int:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT created_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone(),
        )
        if row is not None:
            return cast(int, row["created_at"])
        return self._next_timestamp(connection=connection)

    def _next_timestamp(self, *, connection: sqlite3.Connection) -> int:
        row = cast(
            sqlite3.Row,
            connection.execute(
                "SELECT COALESCE(MAX(updated_at), 0) + 1 AS next_ts FROM sessions"
            ).fetchone(),
        )
        return cast(int, row["next_ts"])
