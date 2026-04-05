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
        user_version = self._read_user_version(connection=connection)
        columns = {
            cast(str, row["name"])
            for row in cast(
                list[sqlite3.Row], connection.execute("PRAGMA table_info(sessions)").fetchall()
            )
        }
        if "pending_approval_json" not in columns:
            _ = connection.execute("ALTER TABLE sessions ADD COLUMN pending_approval_json TEXT")
            user_version = max(user_version, 1)
        if user_version < 2:
            _ = connection.execute("PRAGMA user_version = 2")
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
                    metadata_json, pending_approval_json, created_at, updated_at, last_event_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    metadata_json, pending_approval_json, created_at, updated_at, last_event_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
