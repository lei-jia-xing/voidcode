from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, cast, final, runtime_checkable

from .contracts import (
    RuntimeNotification,
    RuntimeNotificationKind,
    RuntimeNotificationStatus,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionResult,
    UnknownSessionError,
)
from .events import (
    DELEGATED_BACKGROUND_TASK_EVENT_TYPES,
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_QUESTION_REQUESTED,
    EventEnvelope,
    EventSource,
)
from .permission import PendingApproval
from .question import PendingQuestion, PendingQuestionOption, PendingQuestionPrompt
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
    is_background_task_terminal,
    is_background_task_transition_allowed,
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

    def has_session(self, *, workspace: Path, session_id: str) -> bool: ...

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse: ...

    def load_session_result(self, *, workspace: Path, session_id: str) -> RuntimeSessionResult: ...

    def list_notifications(self, *, workspace: Path) -> tuple[RuntimeNotification, ...]: ...

    def acknowledge_notification(
        self, *, workspace: Path, notification_id: str
    ) -> RuntimeNotification: ...

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

    def save_pending_question(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
    ) -> None: ...

    def load_pending_question(
        self, *, workspace: Path, session_id: str
    ) -> PendingQuestion | None: ...

    def clear_pending_question(self, *, workspace: Path, session_id: str) -> None: ...

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

    def list_background_tasks_by_parent_session(
        self, *, workspace: Path, parent_session_id: str
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
        include_queued: bool = True,
    ) -> tuple[BackgroundTaskState, ...]: ...

    def prune_sessions(
        self,
        *,
        workspace: Path,
        keep_last_n: int = 50,
        older_than_ms: int | None = None,
    ) -> int: ...

    def prune_events(
        self,
        *,
        workspace: Path,
        keep_last_per_session: int = 500,
        older_than_ms: int | None = None,
    ) -> int: ...

    def prune_notifications(
        self,
        *,
        workspace: Path,
        keep_last_n: int = 100,
        older_than_ms: int | None = None,
    ) -> int: ...

    def prune_background_tasks(
        self,
        *,
        workspace: Path,
        keep_last_n: int = 50,
        older_than_ms: int | None = None,
    ) -> int: ...


@runtime_checkable
class SessionEventAppender(Protocol):
    def append_session_event(
        self,
        *,
        workspace: Path,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope | None: ...


@final
class SqliteSessionStore:
    _database_path: Path | None
    _RESUME_CHECKPOINT_KINDS = frozenset({"approval_wait", "question_wait", "terminal"})

    _CANONICAL_SCHEMA: dict[str, tuple[tuple[str, str, int, str | None, int], ...]] = {
        "sessions": (
            ("session_id", "TEXT", 0, None, 1),
            ("parent_session_id", "TEXT", 0, None, 0),
            ("workspace", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("turn", "INTEGER", 1, None, 0),
            ("prompt", "TEXT", 1, None, 0),
            ("output", "TEXT", 0, None, 0),
            ("metadata_json", "TEXT", 1, None, 0),
            ("pending_approval_json", "TEXT", 0, None, 0),
            ("pending_question_json", "TEXT", 0, None, 0),
            ("resume_checkpoint_json", "TEXT", 0, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("updated_at", "INTEGER", 1, None, 0),
            ("last_event_sequence", "INTEGER", 1, None, 0),
        ),
        "session_events": (
            ("session_id", "TEXT", 1, None, 1),
            ("sequence", "INTEGER", 1, None, 2),
            ("event_type", "TEXT", 1, None, 0),
            ("source", "TEXT", 1, None, 0),
            ("payload_json", "TEXT", 1, None, 0),
        ),
        "background_tasks": (
            ("task_id", "TEXT", 0, None, 1),
            ("workspace", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("prompt", "TEXT", 1, None, 0),
            ("request_session_id", "TEXT", 0, None, 0),
            ("request_parent_session_id", "TEXT", 0, None, 0),
            ("request_metadata_json", "TEXT", 1, None, 0),
            ("requested_child_session_id", "TEXT", 0, None, 0),
            ("routing_mode", "TEXT", 0, None, 0),
            ("routing_category", "TEXT", 0, None, 0),
            ("routing_subagent_type", "TEXT", 0, None, 0),
            ("routing_description", "TEXT", 0, None, 0),
            ("routing_command", "TEXT", 0, None, 0),
            ("approval_request_id", "TEXT", 0, None, 0),
            ("question_request_id", "TEXT", 0, None, 0),
            ("cancellation_cause", "TEXT", 0, None, 0),
            ("result_available", "INTEGER", 1, "0", 0),
            ("allocate_session_id", "INTEGER", 1, None, 0),
            ("session_id", "TEXT", 0, None, 0),
            ("error", "TEXT", 0, None, 0),
            ("cancel_requested_at", "INTEGER", 0, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("updated_at", "INTEGER", 1, None, 0),
            ("started_at", "INTEGER", 0, None, 0),
            ("finished_at", "INTEGER", 0, None, 0),
        ),
        "session_notifications": (
            ("notification_id", "TEXT", 0, None, 1),
            ("workspace", "TEXT", 1, None, 0),
            ("session_id", "TEXT", 1, None, 0),
            ("kind", "TEXT", 1, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("summary", "TEXT", 1, None, 0),
            ("payload_json", "TEXT", 1, None, 0),
            ("event_sequence", "INTEGER", 1, None, 0),
            ("dedupe_key", "TEXT", 1, None, 0),
            ("created_at", "INTEGER", 1, None, 0),
            ("acknowledged_at", "INTEGER", 0, None, 0),
        ),
        "session_event_deliveries": (
            ("workspace", "TEXT", 1, None, 1),
            ("session_id", "TEXT", 1, None, 2),
            ("dedupe_key", "TEXT", 1, None, 3),
            ("delivered_at", "INTEGER", 1, None, 0),
        ),
    }
    _CANONICAL_UNIQUE_INDEXES: dict[str, frozenset[tuple[str, ...]]] = {
        "sessions": frozenset(),
        "session_events": frozenset(),
        "background_tasks": frozenset(),
        "session_notifications": frozenset({("workspace", "dedupe_key")}),
        "session_event_deliveries": frozenset(),
    }

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
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._ensure_schema(connection=connection, database_path=database_path)
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, *, connection: sqlite3.Connection, database_path: Path) -> None:
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                turn INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                output TEXT,
                metadata_json TEXT NOT NULL,
                pending_approval_json TEXT,
                pending_question_json TEXT,
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
                request_parent_session_id TEXT,
                request_metadata_json TEXT NOT NULL,
                requested_child_session_id TEXT,
                routing_mode TEXT,
                routing_category TEXT,
                routing_subagent_type TEXT,
                routing_description TEXT,
                routing_command TEXT,
                approval_request_id TEXT,
                question_request_id TEXT,
                cancellation_cause TEXT,
                result_available INTEGER NOT NULL DEFAULT 0,
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
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_notifications (
                notification_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                acknowledged_at INTEGER,
                UNIQUE(workspace, dedupe_key)
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_event_deliveries (
                workspace TEXT NOT NULL,
                session_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                delivered_at INTEGER NOT NULL,
                PRIMARY KEY (workspace, session_id, dedupe_key)
            )
            """
        )
        self._assert_canonical_schema(connection=connection, database_path=database_path)
        connection.commit()

    @classmethod
    def _assert_canonical_schema(
        cls, *, connection: sqlite3.Connection, database_path: Path
    ) -> None:
        existing_tables = {
            cast(str, row["name"])
            for row in cast(
                list[sqlite3.Row],
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall(),
            )
        }
        missing_tables = sorted(set(cls._CANONICAL_SCHEMA) - existing_tables)
        if missing_tables:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"missing tables: {', '.join(missing_tables)}",
            )
        for table_name, expected_columns in cls._CANONICAL_SCHEMA.items():
            cls._assert_canonical_table_shape(
                connection=connection,
                database_path=database_path,
                table_name=table_name,
                expected_columns=expected_columns,
            )
        for table_name, expected_indexes in cls._CANONICAL_UNIQUE_INDEXES.items():
            cls._assert_canonical_unique_indexes(
                connection=connection,
                database_path=database_path,
                table_name=table_name,
                expected_indexes=expected_indexes,
            )

    @classmethod
    def _assert_canonical_table_shape(
        cls,
        *,
        connection: sqlite3.Connection,
        database_path: Path,
        table_name: str,
        expected_columns: tuple[tuple[str, str, int, str | None, int], ...],
    ) -> None:
        actual_columns = cls._table_columns(connection=connection, table_name=table_name)
        expected_column_names = {column[0] for column in expected_columns}
        actual_column_names = {column[0] for column in actual_columns}
        missing_columns = sorted(expected_column_names - actual_column_names)
        if missing_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"table '{table_name}' missing columns: {', '.join(missing_columns)}",
            )
        unexpected_columns = sorted(actual_column_names - expected_column_names)
        if unexpected_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=(
                    f"table '{table_name}' has unexpected columns: {', '.join(unexpected_columns)}"
                ),
            )
        if actual_columns != expected_columns:
            cls._raise_schema_mismatch(
                database_path=database_path,
                detail=f"table '{table_name}' shape does not match canonical runtime schema",
            )

    @classmethod
    def _assert_canonical_unique_indexes(
        cls,
        *,
        connection: sqlite3.Connection,
        database_path: Path,
        table_name: str,
        expected_indexes: frozenset[tuple[str, ...]],
    ) -> None:
        actual_indexes = cls._table_unique_indexes(connection=connection, table_name=table_name)
        if actual_indexes == expected_indexes:
            return
        expected = ", ".join("(" + ", ".join(index) + ")" for index in sorted(expected_indexes))
        actual = ", ".join("(" + ", ".join(index) + ")" for index in sorted(actual_indexes))
        cls._raise_schema_mismatch(
            database_path=database_path,
            detail=(
                f"table '{table_name}' unique indexes do not match canonical runtime schema: "
                f"expected [{expected}] got [{actual}]"
            ),
        )

    @staticmethod
    def _table_columns(
        *, connection: sqlite3.Connection, table_name: str
    ) -> tuple[tuple[str, str, int, str | None, int], ...]:
        return tuple(
            (
                cast(str, row["name"]),
                cast(str, row["type"]),
                cast(int, row["notnull"]),
                cast(str | None, row["dflt_value"]),
                cast(int, row["pk"]),
            )
            for row in cast(
                list[sqlite3.Row],
                connection.execute(f"PRAGMA table_info({table_name})").fetchall(),
            )
        )

    @staticmethod
    def _table_unique_indexes(
        *, connection: sqlite3.Connection, table_name: str
    ) -> frozenset[tuple[str, ...]]:
        return frozenset(
            tuple(
                cast(str, column_row["name"])
                for column_row in cast(
                    list[sqlite3.Row],
                    connection.execute(
                        f"PRAGMA index_info({cast(str, index_row['name'])})"
                    ).fetchall(),
                )
            )
            for index_row in cast(
                list[sqlite3.Row],
                connection.execute(f"PRAGMA index_list({table_name})").fetchall(),
            )
            if cast(int, index_row["unique"]) == 1 and cast(str, index_row["origin"]) == "u"
        )

    @staticmethod
    def _raise_schema_mismatch(*, database_path: Path, detail: str) -> None:
        raise RuntimeError(
            "sqlite runtime schema mismatch: "
            f"{detail}. Remove '{database_path}' and rerun to reset local runtime storage."
        )

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

    @staticmethod
    def _session_last_event_sequence(events: tuple[EventEnvelope, ...]) -> int:
        return events[-1].sequence if events else 0

    @staticmethod
    def _session_events_payload(
        events: tuple[EventEnvelope, ...],
    ) -> list[tuple[str, int, str, str, str]]:
        return [
            (
                event.session_id,
                event.sequence,
                event.event_type,
                event.source,
                json.dumps(event.payload, sort_keys=True),
            )
            for event in events
        ]

    def _replace_session_events(
        self,
        *,
        connection: sqlite3.Connection,
        session_id: str,
        events: tuple[EventEnvelope, ...],
    ) -> None:
        _ = connection.execute("DELETE FROM session_events WHERE session_id = ?", (session_id,))
        _ = connection.executemany(
            """
            INSERT INTO session_events (session_id, sequence, event_type, source, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            self._session_events_payload(events),
        )

    def _write_session_snapshot(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval_json: str | None,
        pending_question_json: str | None,
        resume_checkpoint: dict[str, object],
    ) -> int:
        session_id = response.session.session.id
        created_at = self._read_created_at(connection=connection, session_id=session_id)
        updated_at = self._next_timestamp(connection=connection)
        _ = connection.execute(
            """
            INSERT OR REPLACE INTO sessions (
                session_id, parent_session_id, workspace, status, turn, prompt, output,
                metadata_json, pending_approval_json, pending_question_json,
                resume_checkpoint_json, created_at, updated_at,
                last_event_sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                response.session.session.parent_id,
                str(workspace),
                response.session.status,
                response.session.turn,
                request.prompt,
                response.output,
                json.dumps(response.session.metadata, sort_keys=True),
                pending_approval_json,
                pending_question_json,
                json.dumps(resume_checkpoint, sort_keys=True),
                created_at,
                updated_at,
                self._session_last_event_sequence(response.events),
            ),
        )
        self._replace_session_events(
            connection=connection,
            session_id=session_id,
            events=response.events,
        )
        return updated_at

    @staticmethod
    def _checkpoint_skill_snapshot(
        metadata: dict[str, object],
    ) -> tuple[object | None, object | None, dict[str, object]]:
        snapshot_payload = metadata.get("skill_snapshot")
        snapshot = (
            cast(dict[str, object], snapshot_payload) if isinstance(snapshot_payload, dict) else {}
        )
        binding_payload = snapshot.get("binding_snapshot")
        binding_snapshot = (
            cast(dict[str, object], binding_payload) if isinstance(binding_payload, dict) else {}
        )
        return snapshot.get("snapshot_hash"), snapshot.get("snapshot_version"), binding_snapshot

    @classmethod
    def _resume_checkpoint_base(
        cls,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        kind: str,
    ) -> dict[str, object]:
        snapshot_hash, snapshot_version, binding_snapshot = cls._checkpoint_skill_snapshot(
            response.session.metadata
        )
        return {
            "version": 1,
            "kind": kind,
            "prompt": request.prompt,
            "session_status": response.session.status,
            "session_metadata": response.session.metadata,
            "skill_snapshot_hash": snapshot_hash,
            "skill_snapshot_version": snapshot_version,
            "skill_binding_snapshot": binding_snapshot,
            "tool_results": cls._tool_results_from_events(response.events),
            "last_event_sequence": cls._session_last_event_sequence(response.events),
            "output": response.output,
        }

    @staticmethod
    def _decode_json_object_payload(
        payload: str,
        *,
        malformed_message: str,
        non_object_message: str,
    ) -> dict[str, object]:
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(malformed_message) from exc
        if not isinstance(decoded, dict):
            raise ValueError(non_object_message)
        return cast(dict[str, object], decoded)

    @classmethod
    def _decode_resume_checkpoint_payload(cls, payload: str) -> dict[str, object]:
        checkpoint = cls._decode_json_object_payload(
            payload,
            malformed_message="persisted resume checkpoint JSON is malformed",
            non_object_message="persisted resume checkpoint payload must decode to an object",
        )
        kind = checkpoint.get("kind")
        if not isinstance(kind, str) or kind not in cls._RESUME_CHECKPOINT_KINDS:
            raise ValueError(f"persisted resume checkpoint kind is invalid: {kind!r}")
        return checkpoint

    @staticmethod
    def _background_task_runtime_state_defaults() -> dict[str, object]:
        return {
            "approval_request_id": None,
            "question_request_id": None,
            "cancellation_cause": None,
            "result_available": 0,
        }

    @classmethod
    def _request_id_from_pending_payload(cls, payload: str | None) -> str | None:
        if payload is None:
            return None
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            return None
        request_id = cast(dict[str, object], parsed).get("request_id")
        return request_id if isinstance(request_id, str) else None

    @classmethod
    def _background_task_runtime_state_from_session_row(
        cls, row: sqlite3.Row | None
    ) -> dict[str, object]:
        if row is None:
            return cls._background_task_runtime_state_defaults()
        status = cast(str, row["status"])
        return {
            "approval_request_id": cls._request_id_from_pending_payload(
                cast(str | None, row["pending_approval_json"])
            ),
            "question_request_id": cls._request_id_from_pending_payload(
                cast(str | None, row["pending_question_json"])
            ),
            "cancellation_cause": None,
            "result_available": 1 if status in {"waiting", "completed", "failed"} else 0,
        }

    @classmethod
    def _background_task_summary_from_row(cls, row: sqlite3.Row) -> StoredBackgroundTaskSummary:
        return StoredBackgroundTaskSummary(
            task=BackgroundTaskRef(id=cast(str, row["task_id"])),
            status=cls._parse_background_task_status(cast(str, row["status"])),
            prompt=cast(str, row["prompt"]),
            session_id=cast(str | None, row["session_id"]),
            error=cast(str | None, row["error"]),
            created_at=cast(int, row["created_at"]),
            updated_at=cast(int, row["updated_at"]),
        )

    @staticmethod
    def _background_task_durable_payload(row: sqlite3.Row) -> dict[str, object]:
        durable_payload: dict[str, object] = {
            "task_id": cast(str, row["task_id"]),
            "parent_session_id": cast(str | None, row["request_parent_session_id"]),
            "status": cast(str, row["status"]),
            "result_available": bool(cast(int, row["result_available"])),
        }
        optional_fields: tuple[tuple[str, str], ...] = (
            ("requested_child_session_id", "requested_child_session_id"),
            ("child_session_id", "session_id"),
            ("approval_request_id", "approval_request_id"),
            ("question_request_id", "question_request_id"),
            ("routing_mode", "routing_mode"),
            ("routing_category", "routing_category"),
            ("routing_subagent_type", "routing_subagent_type"),
            ("routing_description", "routing_description"),
            ("routing_command", "routing_command"),
            ("cancellation_cause", "cancellation_cause"),
        )
        for payload_key, row_key in optional_fields:
            value = row[row_key]
            if value is not None:
                durable_payload[payload_key] = cast(object, value)
        return durable_payload

    @staticmethod
    def _pending_question_payload(pending_question: PendingQuestion) -> dict[str, object]:
        return {
            "request_id": pending_question.request_id,
            "tool_name": pending_question.tool_name,
            "arguments": pending_question.arguments,
            "prompts": [
                {
                    "question": prompt.question,
                    "header": prompt.header,
                    "multiple": prompt.multiple,
                    "options": [
                        {"label": option.label, "description": option.description}
                        for option in prompt.options
                    ],
                }
                for prompt in pending_question.prompts
            ],
        }

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
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=(
                    None
                    if clear_pending_approval
                    else self._read_pending_approval_json(
                        connection=connection, session_id=session_id
                    )
                ),
                pending_question_json=None,
                resume_checkpoint=self._terminal_resume_checkpoint(
                    request=request,
                    response=response,
                ),
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=None,
                notification_run_id=updated_at,
            )
            connection.commit()

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                SELECT session_id, parent_session_id, status, turn, prompt, updated_at
                FROM sessions
                WHERE workspace = ?
                ORDER BY updated_at DESC, session_id ASC
                """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(
            StoredSessionSummary(
                session=SessionRef(
                    id=cast(str, row["session_id"]),
                    parent_id=cast(str | None, row["parent_session_id"]),
                ),
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
        with self._connect(workspace) as connection:
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=json.dumps(asdict(pending_approval), sort_keys=True),
                pending_question_json=None,
                resume_checkpoint=self._approval_wait_resume_checkpoint(
                    request=request,
                    response=response,
                    pending_approval=pending_approval,
                ),
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                approval_request_id=pending_approval.request_id,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=pending_approval,
                notification_run_id=updated_at,
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
            raise UnknownSessionError(f"unknown session: {session_id}")
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
            request_event_sequence=(
                cast(int, data["request_event_sequence"])
                if isinstance(data.get("request_event_sequence"), int)
                else None
            ),
            owner_session_id=(
                cast(str, data["owner_session_id"])
                if isinstance(data.get("owner_session_id"), str)
                else None
            ),
            owner_parent_session_id=(
                cast(str, data["owner_parent_session_id"])
                if isinstance(data.get("owner_parent_session_id"), str)
                else None
            ),
            delegated_task_id=(
                cast(str, data["delegated_task_id"])
                if isinstance(data.get("delegated_task_id"), str)
                else None
            ),
        )

    def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None:
        with self._connect(workspace) as connection:
            _ = connection.execute(
                "UPDATE sessions SET pending_approval_json = NULL WHERE workspace = ? AND session_id = ?",  # noqa: E501
                (str(workspace), session_id),
            )
            connection.commit()

    def save_pending_question(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
    ) -> None:
        with self._connect(workspace) as connection:
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=None,
                pending_question_json=json.dumps(
                    self._pending_question_payload(pending_question), sort_keys=True
                ),
                resume_checkpoint=self._question_wait_resume_checkpoint(
                    request=request,
                    response=response,
                    pending_question=pending_question,
                ),
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                question_request_id=pending_question.request_id,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=None,
                pending_question=pending_question,
                notification_run_id=updated_at,
            )
            connection.commit()

    def load_pending_question(self, *, workspace: Path, session_id: str) -> PendingQuestion | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT pending_question_json
                    FROM sessions
                    WHERE workspace = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["pending_question_json"])
        if payload is None:
            return None
        data = cast(dict[str, object], json.loads(payload))
        raw_prompts = cast(list[object], data.get("prompts", []))
        prompts: list[PendingQuestionPrompt] = []
        for raw_prompt in raw_prompts:
            prompt_payload = cast(dict[str, object], raw_prompt)
            raw_options = cast(list[object], prompt_payload.get("options", []))
            options = tuple(
                PendingQuestionOption(
                    label=cast(str, cast(dict[str, object], option)["label"]),
                    description=cast(str, cast(dict[str, object], option).get("description", "")),
                )
                for option in raw_options
            )
            prompts.append(
                PendingQuestionPrompt(
                    question=cast(str, prompt_payload["question"]),
                    header=cast(str, prompt_payload["header"]),
                    options=options,
                    multiple=bool(prompt_payload.get("multiple", False)),
                )
            )
        return PendingQuestion(
            request_id=cast(str, data["request_id"]),
            tool_name=cast(str, data.get("tool_name", "question")),
            arguments=cast(dict[str, object], data.get("arguments", {})),
            prompts=tuple(prompts),
        )

    def clear_pending_question(self, *, workspace: Path, session_id: str) -> None:
        with self._connect(workspace) as connection:
            _ = connection.execute(
                (
                    "UPDATE sessions SET pending_question_json = NULL "
                    "WHERE workspace = ? AND session_id = ?"
                ),
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
            raise UnknownSessionError(f"unknown session: {session_id}")
        payload = cast(str | None, row["resume_checkpoint_json"])
        if payload is None:
            return None
        return self._decode_resume_checkpoint_payload(payload)

    def append_session_event(
        self,
        *,
        workspace: Path,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope | None:
        with self._connect(workspace) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                payload = self._enriched_background_task_event_payload(
                    connection=connection,
                    workspace=workspace,
                    event_type=event_type,
                    payload=payload,
                )
                updated_at = self._next_timestamp(connection=connection)
                sequence_row = cast(
                    sqlite3.Row | None,
                    connection.execute(
                        """
                        UPDATE sessions
                        SET updated_at = ?, last_event_sequence = last_event_sequence + 1
                        WHERE workspace = ? AND session_id = ?
                        RETURNING last_event_sequence
                        """,
                        (updated_at, str(workspace), session_id),
                    ).fetchone(),
                )
                if sequence_row is None:
                    connection.rollback()
                    raise UnknownSessionError(f"unknown session: {session_id}")
                if dedupe_key is not None:
                    delivered_at = self._next_timestamp(connection=connection)
                    inserted_delivery = connection.execute(
                        """
                        INSERT OR IGNORE INTO session_event_deliveries (
                            workspace, session_id, dedupe_key, delivered_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (str(workspace), session_id, dedupe_key, delivered_at),
                    )
                    if inserted_delivery.rowcount == 0:
                        _ = connection.execute(
                            """
                            UPDATE sessions
                            SET updated_at = ?, last_event_sequence = last_event_sequence - 1
                            WHERE workspace = ? AND session_id = ?
                            """,
                            (updated_at, str(workspace), session_id),
                        )
                        connection.commit()
                        return None
                sequence = cast(int, sequence_row["last_event_sequence"])
                event = EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=event_type,
                    source=source,
                    payload=payload,
                )
                _ = connection.execute(
                    """
                    INSERT INTO session_events (session_id, sequence, event_type, source, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.session_id,
                        event.sequence,
                        event.event_type,
                        event.source,
                        json.dumps(event.payload, sort_keys=True),
                    ),
                )
                connection.commit()
                return event
            except Exception:
                connection.rollback()
                raise

    def _sync_background_task_durable_state(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        approval_request_id: str | None = None,
        question_request_id: str | None = None,
    ) -> None:
        background_task_id = response.session.metadata.get("background_task_id")
        if not isinstance(background_task_id, str) or (
            response.session.metadata.get("background_run") is not True
        ):
            return
        routing = request.subagent_routing
        result_available = response.session.status in {"waiting", "completed", "failed"}
        inferred_approval_request_id = approval_request_id
        inferred_question_request_id = question_request_id
        if inferred_approval_request_id is None or inferred_question_request_id is None:
            for event in reversed(response.events):
                request_id = event.payload.get("request_id")
                if not isinstance(request_id, str):
                    continue
                if (
                    event.event_type == RUNTIME_APPROVAL_REQUESTED
                    and inferred_approval_request_id is None
                ):
                    inferred_approval_request_id = request_id
                if (
                    event.event_type == RUNTIME_QUESTION_REQUESTED
                    and inferred_question_request_id is None
                ):
                    inferred_question_request_id = request_id
        updated_at = self._next_background_task_timestamp(connection=connection)
        _ = connection.execute(
            """
            UPDATE background_tasks
            SET requested_child_session_id = COALESCE(requested_child_session_id, ?),
                routing_mode = COALESCE(routing_mode, ?),
                routing_category = COALESCE(routing_category, ?),
                routing_subagent_type = COALESCE(routing_subagent_type, ?),
                routing_description = COALESCE(routing_description, ?),
                routing_command = COALESCE(routing_command, ?),
                approval_request_id = COALESCE(?, approval_request_id),
                question_request_id = COALESCE(?, question_request_id),
                result_available = ?,
                session_id = COALESCE(session_id, ?),
                updated_at = ?
            WHERE workspace = ? AND task_id = ?
            """,
            (
                request.session_id,
                routing.mode if routing is not None else None,
                routing.category if routing is not None else None,
                routing.subagent_type if routing is not None else None,
                routing.description if routing is not None else None,
                routing.command if routing is not None else None,
                inferred_approval_request_id,
                inferred_question_request_id,
                1 if result_available else 0,
                response.session.session.id,
                updated_at,
                str(workspace),
                background_task_id,
            ),
        )

    def _enriched_background_task_event_payload(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        event_type: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        if event_type not in DELEGATED_BACKGROUND_TASK_EVENT_TYPES:
            return payload
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            return payload
        try:
            row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
        except ValueError:
            return payload
        return {**payload, **self._background_task_durable_payload(row)}

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
            **SqliteSessionStore._resume_checkpoint_base(
                request=request,
                response=response,
                kind="approval_wait",
            ),
            "pending_approval_request_id": pending_approval.request_id,
            "pending_approval_tool_name": pending_approval.tool_name,
            "pending_approval_arguments": pending_approval.arguments,
            "pending_approval_request_event_sequence": pending_approval.request_event_sequence,
            "pending_approval_owner_session_id": pending_approval.owner_session_id,
            "pending_approval_owner_parent_session_id": pending_approval.owner_parent_session_id,
            "pending_approval_delegated_task_id": pending_approval.delegated_task_id,
        }

    @staticmethod
    def _question_wait_resume_checkpoint(
        *, request: RuntimeRequest, response: RuntimeResponse, pending_question: PendingQuestion
    ) -> dict[str, object]:
        return {
            **SqliteSessionStore._resume_checkpoint_base(
                request=request,
                response=response,
                kind="question_wait",
            ),
            "pending_question_request_id": pending_question.request_id,
            "pending_question_tool_name": pending_question.tool_name,
            "pending_question_prompts": [
                {
                    "header": prompt.header,
                    "question": prompt.question,
                    "multiple": prompt.multiple,
                    "options": [
                        {
                            "label": option.label,
                            "description": option.description,
                        }
                        for option in prompt.options
                    ],
                }
                for prompt in pending_question.prompts
            ],
        }

    @staticmethod
    def _terminal_resume_checkpoint(
        *, request: RuntimeRequest, response: RuntimeResponse
    ) -> dict[str, object]:
        return SqliteSessionStore._resume_checkpoint_base(
            request=request,
            response=response,
            kind="terminal",
        )

    @staticmethod
    def _tool_results_from_events(events: tuple[EventEnvelope, ...]) -> list[dict[str, object]]:
        tool_results: list[dict[str, object]] = []
        for event in events:
            if event.event_type != "runtime.tool_completed":
                continue
            payload = event.payload
            raw_status = payload.get("status")
            is_err = raw_status == "error"
            if raw_status not in {"ok", "error"}:
                is_err = payload.get("error") is not None
            raw_content = payload.get("content")
            raw_error = payload.get("error")
            tool_results.append(
                {
                    "tool_name": str(payload.get("tool", "unknown")),
                    "content": (
                        str(raw_content) if raw_content is not None and not is_err else None
                    ),
                    "status": "error" if is_err else "ok",
                    "data": payload,
                    "error": str(raw_error) if raw_error is not None and is_err else None,
                }
            )
        return tool_results

    def has_session(self, *, workspace: Path, session_id: str) -> bool:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT 1
                    FROM sessions
                    WHERE workspace = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        return row is not None

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse:
        with self._connect(workspace) as connection:
            session_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                SELECT session_id, parent_session_id, status, turn, output, metadata_json
                FROM sessions
                WHERE workspace = ? AND session_id = ?
                """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if session_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
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
            session=SessionRef(
                id=cast(str, session_row["session_id"]),
                parent_id=cast(str | None, session_row["parent_session_id"]),
            ),
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

    def load_session_result(self, *, workspace: Path, session_id: str) -> RuntimeSessionResult:
        response = self.load_session(workspace=workspace, session_id=session_id)
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT prompt
                    FROM sessions
                    WHERE workspace = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        prompt = cast(str, row["prompt"])
        summary, error = self._result_summary(response=response, prompt=prompt)
        return RuntimeSessionResult(
            session=response.session,
            prompt=prompt,
            status=response.session.status,
            summary=summary,
            output=response.output,
            error=error,
            transcript=response.events,
            last_event_sequence=response.events[-1].sequence if response.events else 0,
        )

    def list_notifications(self, *, workspace: Path) -> tuple[RuntimeNotification, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT notifications.notification_id, notifications.session_id,
                           notifications.kind, notifications.status, notifications.summary,
                           notifications.payload_json, notifications.event_sequence,
                           notifications.created_at, notifications.acknowledged_at,
                           sessions.parent_session_id
                    FROM session_notifications AS notifications
                    LEFT JOIN sessions ON sessions.session_id = notifications.session_id
                                      AND sessions.workspace = notifications.workspace
                    WHERE notifications.workspace = ?
                    ORDER BY notifications.created_at DESC, notifications.notification_id DESC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._notification_from_row(row) for row in rows)

    def create_background_task(
        self,
        *,
        workspace: Path,
        task: BackgroundTaskState,
    ) -> None:
        task_id = validate_background_task_id(task.task.id)
        routing = task.routing_identity
        with self._connect(workspace) as connection:
            linked_session_id = task.session_id or task.request.session_id
            initial_runtime_state = self._linked_session_background_task_runtime_state(
                connection=connection,
                workspace=workspace,
                session_id=linked_session_id,
            )
            timestamp = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                INSERT INTO background_tasks (
                    task_id, workspace, status, prompt, request_session_id,
                    request_parent_session_id, request_metadata_json, requested_child_session_id,
                    routing_mode, routing_category, routing_subagent_type,
                    routing_description, routing_command, approval_request_id,
                    question_request_id, cancellation_cause, result_available,
                    allocate_session_id, session_id, error, cancel_requested_at,
                    created_at, updated_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(workspace),
                    task.status,
                    task.request.prompt,
                    task.request.session_id,
                    task.request.parent_session_id,
                    json.dumps(task.request.metadata, sort_keys=True),
                    task.request.session_id,
                    routing.mode if routing is not None else None,
                    routing.category if routing is not None else None,
                    routing.subagent_type if routing is not None else None,
                    routing.description if routing is not None else None,
                    routing.command if routing is not None else None,
                    initial_runtime_state["approval_request_id"],
                    initial_runtime_state["question_request_id"],
                    initial_runtime_state["cancellation_cause"],
                    initial_runtime_state["result_available"],
                    1 if task.request.allocate_session_id else 0,
                    task.session_id,
                    task.error,
                    task.cancel_requested_at,
                    timestamp,
                    timestamp,
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
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def list_background_tasks_by_parent_session(
        self, *, workspace: Path, parent_session_id: str
    ) -> tuple[StoredBackgroundTaskSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                    FROM background_tasks
                    WHERE workspace = ? AND request_parent_session_id = ?
                    ORDER BY updated_at DESC, task_id ASC
                    """,
                    (str(workspace), parent_session_id),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def mark_background_task_running(
        self,
        *,
        workspace: Path,
        task_id: str,
        session_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if not is_background_task_transition_allowed(
                current_status=current_status,
                next_status="running",
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, session_id = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE workspace = ? AND task_id = ? AND status = 'queued'
                """,
                (
                    "running",
                    session_id,
                    updated_at,
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

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
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if not is_background_task_transition_allowed(
                current_status=current_status,
                next_status=status,
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            cancellation_cause = cast(str | None, current["cancellation_cause"])
            if status == "cancelled" and error is not None:
                cancellation_cause = error
            result_available = 1 if status in ("completed", "failed") else 0
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, error = ?, finished_at = ?, updated_at = ?,
                    cancellation_cause = ?, result_available = ?
                WHERE workspace = ? AND task_id = ?
                """,
                (
                    status,
                    error,
                    updated_at,
                    updated_at,
                    cancellation_cause,
                    result_available,
                    str(workspace),
                    task_id,
                ),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def request_background_task_cancel(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if is_background_task_terminal(current_status):
                connection.commit()
                return self._background_task_state_from_row(current)
            if current_status == "queued":
                updated_at = self._next_background_task_timestamp(connection=connection)
                cancelled = connection.execute(
                    """
                    UPDATE background_tasks
                    SET status = 'cancelled', error = ?, cancellation_cause = ?,
                        result_available = 0, finished_at = ?, updated_at = ?
                    WHERE workspace = ? AND task_id = ? AND status = 'queued'
                    """,
                    (
                        "cancelled before start",
                        "cancelled before start",
                        updated_at,
                        updated_at,
                        str(workspace),
                        task_id,
                    ),
                ).rowcount
                if cancelled == 1:
                    updated_row = self._background_task_runtime_row(
                        connection=connection,
                        workspace=workspace,
                        task_id=task_id,
                    )
                    connection.commit()
                    return self._background_task_state_from_row(updated_row)
                current = self._background_task_runtime_row(
                    connection=connection,
                    workspace=workspace,
                    task_id=task_id,
                )
                current_status = self._parse_background_task_status(cast(str, current["status"]))
                if is_background_task_terminal(current_status):
                    connection.commit()
                    return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET cancel_requested_at = ?, updated_at = ?
                WHERE workspace = ? AND task_id = ? AND status = 'running'
                    AND cancel_requested_at IS NULL
                """,
                (updated_at, updated_at, str(workspace), task_id),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def fail_incomplete_background_tasks(
        self,
        *,
        workspace: Path,
        message: str,
        include_queued: bool = True,
    ) -> tuple[BackgroundTaskState, ...]:
        incomplete_status_predicate = (
            "background_tasks.status IN ('queued', 'running')"
            if include_queued
            else "background_tasks.status = 'running'"
        )
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    f"""
                    SELECT background_tasks.task_id, background_tasks.cancel_requested_at
                    FROM background_tasks
                    LEFT JOIN sessions
                      ON sessions.workspace = background_tasks.workspace
                     AND sessions.session_id = background_tasks.session_id
                    WHERE background_tasks.workspace = ?
                      AND {incomplete_status_predicate}
                      AND NOT (
                          background_tasks.status = 'running'
                          AND background_tasks.session_id IS NOT NULL
                          AND sessions.status = 'waiting'
                          AND (
                              sessions.pending_approval_json IS NOT NULL
                              OR sessions.pending_question_json IS NOT NULL
                          )
                      )
                    ORDER BY background_tasks.updated_at ASC, background_tasks.task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
            if not rows:
                return ()
            reconciled_task_ids: list[str] = []
            for row in rows:
                task_id = cast(str, row["task_id"])
                cancel_requested_at = cast(int | None, row["cancel_requested_at"])
                updated_at = self._next_background_task_timestamp(connection=connection)
                if cancel_requested_at is not None:
                    _ = connection.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'cancelled',
                            error = ?,
                            cancellation_cause = COALESCE(cancellation_cause, ?),
                            finished_at = ?,
                            updated_at = ?,
                            result_available = 0
                        WHERE workspace = ?
                          AND task_id = ?
                          AND status = 'running'
                          AND cancel_requested_at IS NOT NULL
                        """,
                        (
                            "cancelled by parent during delegated execution",
                            "cancelled by parent during delegated execution",
                            updated_at,
                            updated_at,
                            str(workspace),
                            task_id,
                        ),
                    )
                else:
                    _ = connection.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'failed',
                            error = ?,
                            finished_at = ?,
                            updated_at = ?,
                            result_available = 1
                        WHERE workspace = ?
                          AND task_id = ?
                          AND status IN ('queued', 'running')
                          AND cancel_requested_at IS NULL
                        """,
                        (message, updated_at, updated_at, str(workspace), task_id),
                    )
                reconciled_task_ids.append(task_id)
            connection.commit()
        return tuple(
            self.load_background_task(workspace=workspace, task_id=task_id)
            for task_id in reconciled_task_ids
        )

    def persist_background_task_runtime_state(
        self,
        *,
        workspace: Path,
        task_id: str,
        approval_request_id: str | None = None,
        question_request_id: str | None = None,
        result_available: bool | None = None,
        cancellation_cause: str | None = None,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            if is_background_task_terminal(
                self._parse_background_task_status(cast(str, current["status"]))
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET approval_request_id = ?,
                    question_request_id = ?,
                    cancellation_cause = ?,
                    result_available = ?,
                    updated_at = ?
                WHERE workspace = ? AND task_id = ?
                """,
                (
                    approval_request_id
                    if approval_request_id is not None
                    else cast(str | None, current["approval_request_id"]),
                    question_request_id
                    if question_request_id is not None
                    else cast(str | None, current["question_request_id"]),
                    cancellation_cause
                    if cancellation_cause is not None
                    else cast(str | None, current["cancellation_cause"]),
                    (
                        1
                        if result_available
                        else 0
                        if result_available is not None
                        else cast(int, current["result_available"])
                    ),
                    updated_at,
                    str(workspace),
                    task_id,
                ),
            )
            connection.commit()
        return self.load_background_task(workspace=workspace, task_id=task_id)

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
                parent_session_id=cast(str | None, row["request_parent_session_id"]),
                metadata=cast(dict[str, object], metadata),
                allocate_session_id=bool(cast(int, row["allocate_session_id"])),
            ),
            session_id=cast(str | None, row["session_id"]),
            approval_request_id=cast(str | None, row["approval_request_id"]),
            question_request_id=cast(str | None, row["question_request_id"]),
            cancellation_cause=cast(str | None, row["cancellation_cause"]),
            result_available=bool(cast(int, row["result_available"])),
            error=cast(str | None, row["error"]),
            created_at=cast(int, row["created_at"]),
            updated_at=cast(int, row["updated_at"]),
            started_at=cast(int | None, row["started_at"]),
            finished_at=cast(int | None, row["finished_at"]),
            cancel_requested_at=cast(int | None, row["cancel_requested_at"]),
        )

    def _background_task_runtime_row(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        task_id: str,
    ) -> sqlite3.Row:
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
        return row

    def _next_background_task_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return int(time.time_ns() / 1_000_000)

    def prune_sessions(
        self,
        *,
        workspace: Path,
        keep_last_n: int = 50,
        older_than_ms: int | None = None,
    ) -> int:
        with self._connect(workspace) as connection:
            cutoff = older_than_ms
            if cutoff is not None:
                now = self._next_timestamp(connection=connection)
                cutoff = now - cutoff
            deleted = cast(
                sqlite3.Cursor,
                connection.execute(
                    """
                    DELETE FROM sessions
                    WHERE workspace = ?
                      AND session_id NOT IN (
                          SELECT session_id FROM sessions
                          WHERE workspace = ?
                          ORDER BY updated_at DESC, session_id ASC
                          LIMIT ?
                      )
                      AND (1 = ? OR updated_at < ?)
                    """,
                    (
                        str(workspace),
                        str(workspace),
                        keep_last_n,
                        1 if cutoff is None else 0,
                        cutoff if cutoff is not None else 0,
                    ),
                ),
            )
            connection.commit()
            return deleted.rowcount

    def prune_events(
        self,
        *,
        workspace: Path,
        keep_last_per_session: int = 500,
        older_than_ms: int | None = None,
    ) -> int:
        with self._connect(workspace) as connection:
            cutoff = older_than_ms
            if cutoff is not None:
                now = self._next_timestamp(connection=connection)
                cutoff = now - cutoff
            deleted = cast(
                sqlite3.Cursor,
                connection.execute(
                    """
                    DELETE FROM session_events
                    WHERE (session_id, sequence) NOT IN (
                          SELECT session_id, sequence FROM (
                              SELECT session_id, sequence,
                                     ROW_NUMBER() OVER (
                                         PARTITION BY session_id ORDER BY sequence DESC
                                     ) AS rn
                              FROM session_events
                          ) WHERE rn <= ?
                      )
                      AND (1 = ? OR session_id IN (
                          SELECT session_id FROM sessions WHERE workspace = ? AND updated_at < ?
                      ))
                    """,
                    (
                        keep_last_per_session,
                        1 if cutoff is None else 0,
                        str(workspace),
                        cutoff if cutoff is not None else 0,
                    ),
                ),
            )
            connection.commit()
            return deleted.rowcount

    def prune_notifications(
        self,
        *,
        workspace: Path,
        keep_last_n: int = 100,
        older_than_ms: int | None = None,
    ) -> int:
        with self._connect(workspace) as connection:
            cutoff = older_than_ms
            if cutoff is not None:
                now = self._next_timestamp(connection=connection)
                cutoff = now - cutoff
            deleted = cast(
                sqlite3.Cursor,
                connection.execute(
                    """
                    DELETE FROM session_notifications
                    WHERE workspace = ?
                      AND notification_id NOT IN (
                          SELECT notification_id FROM session_notifications
                          WHERE workspace = ?
                          ORDER BY created_at DESC, notification_id DESC
                          LIMIT ?
                      )
                      AND (1 = ? OR created_at < ?)
                    """,
                    (
                        str(workspace),
                        str(workspace),
                        keep_last_n,
                        1 if cutoff is None else 0,
                        cutoff if cutoff is not None else 0,
                    ),
                ),
            )
            connection.commit()
            return deleted.rowcount

    def prune_background_tasks(
        self,
        *,
        workspace: Path,
        keep_last_n: int = 50,
        older_than_ms: int | None = None,
    ) -> int:
        with self._connect(workspace) as connection:
            cutoff = older_than_ms
            if cutoff is not None:
                now = self._next_timestamp(connection=connection)
                cutoff = now - cutoff
            deleted = cast(
                sqlite3.Cursor,
                connection.execute(
                    """
                    DELETE FROM background_tasks
                    WHERE workspace = ?
                      AND task_id NOT IN (
                          SELECT task_id FROM background_tasks
                          WHERE workspace = ?
                          ORDER BY updated_at DESC, task_id ASC
                          LIMIT ?
                      )
                      AND (1 = ? OR updated_at < ?)
                    """,
                    (
                        str(workspace),
                        str(workspace),
                        keep_last_n,
                        1 if cutoff is None else 0,
                        cutoff if cutoff is not None else 0,
                    ),
                ),
            )
            connection.commit()
            return deleted.rowcount

    def _linked_session_background_task_runtime_state(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str | None,
    ) -> dict[str, object]:
        if session_id is None:
            return self._background_task_runtime_state_defaults()
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT status, pending_approval_json, pending_question_json
                FROM sessions
                WHERE workspace = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            ).fetchone(),
        )
        return self._background_task_runtime_state_from_session_row(row)

    def acknowledge_notification(
        self, *, workspace: Path, notification_id: str
    ) -> RuntimeNotification:
        with self._connect(workspace) as connection:
            existing_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT notification_id, session_id, kind, status, summary, payload_json,
                           event_sequence, created_at, acknowledged_at
                    FROM session_notifications
                    WHERE workspace = ? AND notification_id = ?
                    """,
                    (str(workspace), notification_id),
                ).fetchone(),
            )
            if existing_row is None:
                raise ValueError(f"unknown notification: {notification_id}")
            acknowledged_at = cast(int | None, existing_row["acknowledged_at"])
            if acknowledged_at is None:
                acknowledged_at = self._next_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    UPDATE session_notifications
                    SET status = 'acknowledged', acknowledged_at = ?
                    WHERE workspace = ? AND notification_id = ?
                    """,
                    (acknowledged_at, str(workspace), notification_id),
                )
                connection.commit()
            row = cast(
                sqlite3.Row,
                connection.execute(
                    """
                    SELECT notifications.notification_id, notifications.session_id,
                           notifications.kind, notifications.status, notifications.summary,
                           notifications.payload_json, notifications.event_sequence,
                           notifications.created_at, notifications.acknowledged_at,
                           sessions.parent_session_id
                    FROM session_notifications AS notifications
                    LEFT JOIN sessions ON sessions.session_id = notifications.session_id
                                      AND sessions.workspace = notifications.workspace
                    WHERE notifications.workspace = ? AND notifications.notification_id = ?
                    """,
                    (str(workspace), notification_id),
                ).fetchone(),
            )
        return self._notification_from_row(row)

    @staticmethod
    def _result_summary(*, response: RuntimeResponse, prompt: str) -> tuple[str, str | None]:
        if response.session.status == "completed":
            output = (response.output or "").strip()
            if output:
                return f"Completed: {output[:120]}", None
            return f"Completed session for prompt: {prompt[:80]}", None
        if response.session.status == "waiting":
            for event in reversed(response.events):
                if event.event_type == "runtime.approval_requested":
                    tool = str(event.payload.get("tool", "tool"))
                    target = str(event.payload.get("target_summary", "")).strip()
                    if target:
                        return f"Approval blocked on {tool}: {target[:100]}", None
                    return f"Approval blocked on {tool}", None
                if event.event_type == "runtime.question_requested":
                    question_count = event.payload.get("question_count")
                    if isinstance(question_count, int) and question_count > 0:
                        label = "question" if question_count == 1 else "questions"
                        return f"Question blocked on {question_count} {label}", None
                    return "Question blocked", None
            return "Approval blocked", None
        if response.session.status == "failed":
            for event in reversed(response.events):
                if event.event_type == "runtime.failed":
                    error = str(event.payload.get("error", "runtime failed"))
                    return f"Failed: {error[:120]}", error
            return "Failed", None
        return f"{response.session.status.capitalize()} session", None

    def _sync_notifications(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None = None,
        notification_run_id: int,
    ) -> None:
        session_id = response.session.session.id
        notification = self._notification_candidate(
            request=request,
            response=response,
            pending_approval=pending_approval,
            pending_question=pending_question,
            notification_run_id=notification_run_id,
        )
        if pending_approval is None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace = ?
                  AND session_id = ?
                  AND kind = 'approval_blocked'
                  AND status = 'unread'
                """,
                (
                    self._next_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                ),
            )
        elif notification is not None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace = ?
                  AND session_id = ?
                  AND kind = 'approval_blocked'
                  AND status = 'unread'
                  AND notification_id != ?
                """,
                (
                    self._next_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                    notification["notification_id"],
                ),
            )
        if pending_question is None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace = ?
                  AND session_id = ?
                  AND kind = 'question_blocked'
                  AND status = 'unread'
                """,
                (
                    self._next_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                ),
            )
        elif notification is not None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace = ?
                  AND session_id = ?
                  AND kind = 'question_blocked'
                  AND status = 'unread'
                  AND notification_id != ?
                """,
                (
                    self._next_timestamp(connection=connection),
                    str(workspace),
                    session_id,
                    notification["notification_id"],
                ),
            )
        if notification is None:
            return
        _ = connection.execute(
            """
            INSERT OR IGNORE INTO session_notifications (
                notification_id, workspace, session_id, kind, status, summary, payload_json,
                event_sequence, dedupe_key, created_at, acknowledged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notification["notification_id"],
                str(workspace),
                session_id,
                notification["kind"],
                "unread",
                notification["summary"],
                json.dumps(notification["payload"], sort_keys=True),
                notification["event_sequence"],
                notification["dedupe_key"],
                self._next_timestamp(connection=connection),
                None,
            ),
        )

    def _notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval | None,
        pending_question: PendingQuestion | None,
        notification_run_id: int,
    ) -> dict[str, object] | None:
        if pending_approval is not None:
            return self._approval_notification_candidate(
                request=request,
                response=response,
                pending_approval=pending_approval,
            )
        if pending_question is not None:
            return self._question_notification_candidate(
                request=request,
                response=response,
                pending_question=pending_question,
            )
        return self._terminal_notification_candidate(
            request=request,
            response=response,
            notification_run_id=notification_run_id,
        )

    def _approval_notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
    ) -> dict[str, object]:
        session_id = response.session.session.id
        summary, _ = self._result_summary(response=response, prompt=request.prompt)
        event_sequence = self._session_last_event_sequence(response.events)
        dedupe_key = f"{session_id}:approval_blocked:{pending_approval.request_id}"
        return {
            "notification_id": dedupe_key,
            "dedupe_key": dedupe_key,
            "kind": "approval_blocked",
            "summary": summary,
            "event_sequence": event_sequence,
            "payload": {
                "request_id": pending_approval.request_id,
                "tool": pending_approval.tool_name,
                "arguments": pending_approval.arguments,
                "target_summary": pending_approval.target_summary,
                "reason": pending_approval.reason,
            },
        }

    def _question_notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_question: PendingQuestion,
    ) -> dict[str, object]:
        session_id = response.session.session.id
        summary, _ = self._result_summary(response=response, prompt=request.prompt)
        event_sequence = self._session_last_event_sequence(response.events)
        dedupe_key = f"{session_id}:question_blocked:{pending_question.request_id}"
        return {
            "notification_id": dedupe_key,
            "dedupe_key": dedupe_key,
            "kind": "question_blocked",
            "summary": summary,
            "event_sequence": event_sequence,
            "payload": {
                "request_id": pending_question.request_id,
                "questions": [
                    {
                        "header": prompt.header,
                        "question": prompt.question,
                        "multiple": prompt.multiple,
                        "options": [
                            {"label": option.label, "description": option.description}
                            for option in prompt.options
                        ],
                    }
                    for prompt in pending_question.prompts
                ],
            },
        }

    def _terminal_notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        notification_run_id: int,
    ) -> dict[str, object] | None:
        session_id = response.session.session.id
        event_sequence = self._session_last_event_sequence(response.events)
        if response.session.status == "completed":
            summary, _ = self._result_summary(response=response, prompt=request.prompt)
            dedupe_key = f"{session_id}:completion:{notification_run_id}"
            return {
                "notification_id": dedupe_key,
                "dedupe_key": dedupe_key,
                "kind": "completion",
                "summary": summary,
                "event_sequence": event_sequence,
                "payload": {"output": response.output},
            }
        if response.session.status == "failed":
            summary, error = self._result_summary(response=response, prompt=request.prompt)
            dedupe_key = f"{session_id}:failure:{notification_run_id}"
            return {
                "notification_id": dedupe_key,
                "dedupe_key": dedupe_key,
                "kind": "failure",
                "summary": summary,
                "event_sequence": event_sequence,
                "payload": {"error": error},
            }
        return None

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> RuntimeNotification:
        return RuntimeNotification(
            id=cast(str, row["notification_id"]),
            session=SessionRef(
                id=cast(str, row["session_id"]),
                parent_id=cast(str | None, row["parent_session_id"]),
            ),
            kind=cast(RuntimeNotificationKind, row["kind"]),
            status=cast(RuntimeNotificationStatus, row["status"]),
            summary=cast(str, row["summary"]),
            event_sequence=cast(int, row["event_sequence"]),
            created_at=cast(int, row["created_at"]),
            acknowledged_at=cast(int | None, row["acknowledged_at"]),
            payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
        )

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
        return int(time.time_ns() / 1_000_000)
