from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .contracts import (
    RuntimeRequest,
    RuntimeResponse,
)
from .events import (
    DELEGATED_BACKGROUND_TASK_EVENT_TYPES,
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_QUESTION_REQUESTED,
)
from .session import normalize_persisted_session_metadata
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    DelegatedReminderState,
    DelegatedReminderStopCondition,
    StoredBackgroundTaskSummary,
    is_background_task_terminal,
    is_background_task_transition_allowed,
    validate_background_task_id,
)

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _BackgroundTaskStorageMixin(_MixinBase):
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
    def _background_task_runtime_state_from_session_row(cls, row: sqlite3.Row | None) -> dict[str, object]:
        if row is None:
            return cls._background_task_runtime_state_defaults()
        status = cast(str, row["status"])
        return {
            "approval_request_id": cls._request_id_from_pending_payload(cast(str | None, row["pending_approval_json"])),
            "question_request_id": cls._request_id_from_pending_payload(cast(str | None, row["pending_question_json"])),
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
            created_at_unix_ms=cast(int | None, row["created_at_unix_ms"]),
            keep_alive=bool(cast(int, row["keep_alive"])),
            steer_prompt=cast(str | None, row["steer_prompt"]),
        )

    def _background_task_durable_payload(self, row: sqlite3.Row) -> dict[str, object]:
        durable_payload: dict[str, object] = {
            "task_id": cast(str, row["task_id"]),
            "parent_session_id": cast(str | None, row["request_parent_session_id"]),
            "status": cast(str, row["status"]),
            "result_available": bool(cast(int, row["result_available"])),
        }
        reminder_state = self._delegated_reminder_state_from_payload(cast(str | None, row["delegated_reminder_json"]))
        if reminder_state is not None:
            durable_payload["delegated_reminder"] = self._delegated_reminder_state_payload(reminder_state)
        optional_fields: tuple[tuple[str, str], ...] = (
            ("requested_child_session_id", "requested_child_session_id"),
            ("child_session_id", "session_id"),
            ("approval_request_id", "approval_request_id"),
            ("question_request_id", "question_request_id"),
            ("routing_mode", "routing_mode"),
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
    def _delegated_reminder_state_payload(state: DelegatedReminderState) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": state.task_id,
            "eligible": state.eligible,
            "already_sent_for_idle_episode": state.already_sent_for_idle_episode,
        }
        optional_fields: tuple[tuple[str, object | None], ...] = (
            ("parent_session_id", state.parent_session_id),
            ("child_session_id", state.child_session_id),
            ("idle_episode_id", state.idle_episode_id),
            ("idle_detected_at_unix_ms", state.idle_detected_at_unix_ms),
            ("reminder_sent_at_unix_ms", state.reminder_sent_at_unix_ms),
            ("stopped_at_unix_ms", state.stopped_at_unix_ms),
            ("stop_condition", state.stop_condition),
        )
        for key, value in optional_fields:
            if value is not None:
                payload[key] = value
        return payload

    @staticmethod
    def _parse_delegated_reminder_stop_condition(
        value: object,
    ) -> DelegatedReminderStopCondition | None:
        if value is None:
            return None
        if value in {
            "result_read",
            "explicit_retry",
            "cancellation",
            "terminal_status",
            "already_sent_for_idle_episode",
        }:
            return cast(DelegatedReminderStopCondition, value)
        raise ValueError(f"invalid delegated reminder stop condition: {value!r}")

    @classmethod
    def _delegated_reminder_state_from_payload(cls, payload: str | None) -> DelegatedReminderState | None:
        if payload is None:
            return None
        decoded = cls._decode_json_object_payload(
            payload,
            malformed_message="persisted delegated reminder JSON is malformed",
            non_object_message="persisted delegated reminder payload must decode to an object",
        )
        task_id = decoded.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("persisted delegated reminder task_id must be a non-empty string")
        stop_condition = cls._parse_delegated_reminder_stop_condition(decoded.get("stop_condition"))
        return DelegatedReminderState(
            task_id=task_id,
            parent_session_id=cls._optional_string(decoded.get("parent_session_id")),
            child_session_id=cls._optional_string(decoded.get("child_session_id")),
            idle_episode_id=cls._optional_string(decoded.get("idle_episode_id")),
            idle_detected_at_unix_ms=cls._optional_int(decoded.get("idle_detected_at_unix_ms")),
            reminder_sent_at_unix_ms=cls._optional_int(decoded.get("reminder_sent_at_unix_ms")),
            stopped_at_unix_ms=cls._optional_int(decoded.get("stopped_at_unix_ms")),
            stop_condition=stop_condition,
        )

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
        if not isinstance(background_task_id, str) or (response.session.metadata.get("background_run") is not True):
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
                if event.event_type == RUNTIME_APPROVAL_REQUESTED and inferred_approval_request_id is None:
                    inferred_approval_request_id = request_id
                if event.event_type == RUNTIME_QUESTION_REQUESTED and inferred_question_request_id is None:
                    inferred_question_request_id = request_id
        updated_at = self._next_background_task_timestamp(connection=connection)
        _ = connection.execute(
            """
            UPDATE background_tasks
            SET requested_child_session_id = COALESCE(requested_child_session_id, ?),
                routing_mode = COALESCE(routing_mode, ?),
                routing_subagent_type = COALESCE(routing_subagent_type, ?),
                routing_description = COALESCE(routing_description, ?),
                routing_command = COALESCE(routing_command, ?),
                approval_request_id = COALESCE(?, approval_request_id),
                question_request_id = COALESCE(?, question_request_id),
                result_available = ?,
                session_id = COALESCE(session_id, ?),
                updated_at = ?
            WHERE workspace_id = ? AND task_id = ?
            """,
            (
                request.session_id,
                routing.mode if routing is not None else None,
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

    def _stop_delegated_reminder_state(
        self,
        *,
        existing_payload: str | None,
        stop_condition: DelegatedReminderStopCondition,
        stopped_at_unix_ms: int,
    ) -> str | None:
        reminder_state = self._delegated_reminder_state_from_payload(existing_payload)
        if reminder_state is None:
            return None
        if reminder_state.stop_condition is not None and stop_condition == "already_sent_for_idle_episode":
            return existing_payload
        stopped_state = DelegatedReminderState(
            task_id=reminder_state.task_id,
            parent_session_id=reminder_state.parent_session_id,
            child_session_id=reminder_state.child_session_id,
            idle_episode_id=reminder_state.idle_episode_id,
            idle_detected_at_unix_ms=reminder_state.idle_detected_at_unix_ms,
            reminder_sent_at_unix_ms=reminder_state.reminder_sent_at_unix_ms,
            stopped_at_unix_ms=stopped_at_unix_ms,
            stop_condition=stop_condition,
        )
        return json.dumps(
            self._delegated_reminder_state_payload(stopped_state),
            sort_keys=True,
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

    def create_background_task(
        self,
        *,
        workspace: Path,
        task: BackgroundTaskState,
    ) -> None:
        task_id = validate_background_task_id(task.task.id)
        routing = task.routing_identity
        with self._write_connect(workspace) as connection:
            linked_session_id = task.session_id or task.request.session_id
            initial_runtime_state = self._linked_session_background_task_runtime_state(
                connection=connection,
                workspace=workspace,
                session_id=linked_session_id,
            )
            timestamp = self._next_background_task_timestamp(connection=connection)
            wall_clock_ms = self._current_unix_ms()
            _ = connection.execute(
                """
                INSERT INTO background_tasks (
                    task_id, workspace_id, status, prompt, request_session_id,
                    request_parent_session_id, request_metadata_json, requested_child_session_id,
                    routing_mode, routing_subagent_type,
                    routing_description, routing_command, approval_request_id,
                    question_request_id, cancellation_cause, result_available,
                    delegated_reminder_json, allocate_session_id, session_id,
                    error, cancel_requested_at,
                    created_at, updated_at, started_at, finished_at,
                    created_at_unix_ms, started_at_unix_ms, finished_at_unix_ms, keep_alive
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
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
                    routing.subagent_type if routing is not None else None,
                    routing.description if routing is not None else None,
                    routing.command if routing is not None else None,
                    initial_runtime_state["approval_request_id"],
                    initial_runtime_state["question_request_id"],
                    initial_runtime_state["cancellation_cause"],
                    initial_runtime_state["result_available"],
                    None,
                    1 if task.request.allocate_session_id else 0,
                    task.session_id,
                    task.error,
                    task.cancel_requested_at,
                    timestamp,
                    timestamp,
                    task.started_at,
                    task.finished_at,
                    wall_clock_ms,
                    task.started_at_unix_ms,
                    task.finished_at_unix_ms,
                    1 if task.request.metadata.get("keep_alive") is True else 0,
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
                    WHERE workspace_id = ? AND task_id = ?
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
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ?
                    ORDER BY updated_at DESC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def list_queued_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ? AND status = 'queued'
                    ORDER BY created_at ASC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def list_running_background_tasks(self, *, workspace: Path) -> tuple[StoredBackgroundTaskSummary, ...]:
        """Status-indexed running-task summaries for worker-liveness reconciliation.

        Bounded by the task concurrency limit; unlike ``list_background_tasks``
        this never scans terminal history, so hot read paths (task loads,
        observability) can check worker liveness without a full-history pass.
        """
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ? AND status = 'running'
                    ORDER BY updated_at ASC, task_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def list_background_tasks_by_parent_session(self, *, workspace: Path, parent_session_id: str) -> tuple[StoredBackgroundTaskSummary, ...]:
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT task_id, status, prompt, session_id, error, created_at, updated_at
                           , created_at_unix_ms, keep_alive, steer_prompt
                    FROM background_tasks
                    WHERE workspace_id = ? AND request_parent_session_id = ?
                    ORDER BY updated_at DESC, task_id ASC
                    """,
                    (str(workspace), parent_session_id),
                ).fetchall(),
            )
        return tuple(self._background_task_summary_from_row(row) for row in rows)

    def load_background_task_by_child_session(
        self,
        *,
        workspace: Path,
        child_session_id: str,
    ) -> BackgroundTaskState | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT *
                    FROM background_tasks
                    WHERE workspace_id = ? AND session_id = ?
                    ORDER BY updated_at DESC, task_id DESC
                    LIMIT 1
                    """,
                    (str(workspace), child_session_id),
                ).fetchone(),
            )
        if row is None:
            return None
        return self._background_task_state_from_row(row)

    def mark_background_task_running(
        self,
        *,
        workspace: Path,
        task_id: str,
        session_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
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
            started_at_unix_ms = self._current_unix_ms()
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, session_id = ?, started_at = COALESCE(started_at, ?),
                    started_at_unix_ms = COALESCE(started_at_unix_ms, ?), updated_at = ?
                WHERE workspace_id = ? AND task_id = ? AND status = 'queued'
                """,
                (
                    "running",
                    session_id,
                    updated_at,
                    started_at_unix_ms,
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
        if not is_background_task_terminal(status):
            raise ValueError("background task terminal status must be completed, failed, cancelled, or interrupted")
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
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
            result_available = 1 if status in ("completed", "failed", "interrupted") else 0
            updated_at = self._next_background_task_timestamp(connection=connection)
            finished_at_unix_ms = self._current_unix_ms()
            delegated_reminder_json = self._stop_delegated_reminder_state(
                existing_payload=cast(str | None, current["delegated_reminder_json"]),
                stop_condition="terminal_status",
                stopped_at_unix_ms=finished_at_unix_ms,
            )
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, error = ?, finished_at = ?, updated_at = ?,
                    cancellation_cause = ?, result_available = ?, finished_at_unix_ms = ?,
                    delegated_reminder_json = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    status,
                    error,
                    updated_at,
                    updated_at,
                    cancellation_cause,
                    result_available,
                    finished_at_unix_ms,
                    delegated_reminder_json,
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

    def mark_background_task_idle(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if not is_background_task_transition_allowed(
                current_status=current_status,
                next_status="idle",
            ):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET status = ?, steer_prompt = NULL, updated_at = ?
                WHERE workspace_id = ? AND task_id = ? AND status = 'running'
                """,
                (
                    "idle",
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

    def mark_background_task_steered(
        self,
        *,
        workspace: Path,
        task_id: str,
        steer_prompt: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
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
                SET status = ?, steer_prompt = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ? AND status IN ('idle', 'interrupted')
                """,
                (
                    "running",
                    steer_prompt,
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

    def request_background_task_cancel(
        self,
        *,
        workspace: Path,
        task_id: str,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
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
                stopped_at_unix_ms = self._current_unix_ms()
                delegated_reminder_json = self._stop_delegated_reminder_state(
                    existing_payload=cast(str | None, current["delegated_reminder_json"]),
                    stop_condition="cancellation",
                    stopped_at_unix_ms=stopped_at_unix_ms,
                )
                cancelled = connection.execute(
                    """
                    UPDATE background_tasks
                    SET status = 'cancelled', error = ?, cancellation_cause = ?,
                        result_available = 0, finished_at = ?, updated_at = ?,
                        delegated_reminder_json = ?
                    WHERE workspace_id = ? AND task_id = ? AND status = 'queued'
                    """,
                    (
                        "cancelled before start",
                        "cancelled before start",
                        updated_at,
                        updated_at,
                        delegated_reminder_json,
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
            delegated_reminder_json = self._stop_delegated_reminder_state(
                existing_payload=cast(str | None, current["delegated_reminder_json"]),
                stop_condition="cancellation",
                stopped_at_unix_ms=self._current_unix_ms(),
            )
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET cancel_requested_at = ?, updated_at = ?, delegated_reminder_json = ?
                WHERE workspace_id = ? AND task_id = ? AND status = 'running'
                    AND cancel_requested_at IS NULL
                """,
                (updated_at, updated_at, delegated_reminder_json, str(workspace), task_id),
            )
            updated_row = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            connection.commit()
        return self._background_task_state_from_row(updated_row)

    def record_background_task_idle_reminder_eligible(
        self,
        *,
        workspace: Path,
        task_id: str,
        child_session_id: str,
        idle_episode_id: str,
        idle_detected_at_unix_ms: int,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            current_status = self._parse_background_task_status(cast(str, current["status"]))
            if is_background_task_terminal(current_status):
                connection.commit()
                return self._background_task_state_from_row(current)
            existing_state = self._delegated_reminder_state_from_payload(cast(str | None, current["delegated_reminder_json"]))
            if existing_state is not None and existing_state.stop_condition in {
                "result_read",
                "explicit_retry",
                "cancellation",
                "terminal_status",
            }:
                connection.commit()
                return self._background_task_state_from_row(current)
            if existing_state is not None and existing_state.idle_episode_id == idle_episode_id:
                connection.commit()
                return self._background_task_state_from_row(current)
            reminder_state = DelegatedReminderState(
                task_id=task_id,
                parent_session_id=cast(str | None, current["request_parent_session_id"]),
                child_session_id=child_session_id,
                idle_episode_id=idle_episode_id,
                idle_detected_at_unix_ms=idle_detected_at_unix_ms,
            )
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET delegated_reminder_json = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    json.dumps(
                        self._delegated_reminder_state_payload(reminder_state),
                        sort_keys=True,
                    ),
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

    def mark_background_task_idle_reminder_sent(
        self,
        *,
        workspace: Path,
        task_id: str,
        idle_episode_id: str,
        reminder_sent_at_unix_ms: int,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            reminder_state = self._delegated_reminder_state_from_payload(cast(str | None, current["delegated_reminder_json"]))
            if reminder_state is None or reminder_state.idle_episode_id != idle_episode_id:
                connection.commit()
                return self._background_task_state_from_row(current)
            if reminder_state.stop_condition not in (None, "already_sent_for_idle_episode"):
                connection.commit()
                return self._background_task_state_from_row(current)
            sent_state = DelegatedReminderState(
                task_id=reminder_state.task_id,
                parent_session_id=reminder_state.parent_session_id,
                child_session_id=reminder_state.child_session_id,
                idle_episode_id=reminder_state.idle_episode_id,
                idle_detected_at_unix_ms=reminder_state.idle_detected_at_unix_ms,
                reminder_sent_at_unix_ms=reminder_sent_at_unix_ms,
                stopped_at_unix_ms=reminder_sent_at_unix_ms,
                stop_condition="already_sent_for_idle_episode",
            )
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET delegated_reminder_json = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    json.dumps(self._delegated_reminder_state_payload(sent_state), sort_keys=True),
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

    def stop_background_task_idle_reminder(
        self,
        *,
        workspace: Path,
        task_id: str,
        stop_condition: DelegatedReminderStopCondition,
    ) -> BackgroundTaskState:
        task_id = validate_background_task_id(task_id)
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            delegated_reminder_json = self._stop_delegated_reminder_state(
                existing_payload=cast(str | None, current["delegated_reminder_json"]),
                stop_condition=stop_condition,
                stopped_at_unix_ms=self._current_unix_ms(),
            )
            if delegated_reminder_json == cast(str | None, current["delegated_reminder_json"]):
                connection.commit()
                return self._background_task_state_from_row(current)
            updated_at = self._next_background_task_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE background_tasks
                SET delegated_reminder_json = ?, updated_at = ?
                WHERE workspace_id = ? AND task_id = ?
                """,
                (delegated_reminder_json, updated_at, str(workspace), task_id),
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
        incomplete_status_predicate = "background_tasks.status IN ('queued', 'running')" if include_queued else "background_tasks.status = 'running'"
        with self._write_connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    f"""
                    SELECT background_tasks.task_id, background_tasks.cancel_requested_at,
                           background_tasks.delegated_reminder_json
                    FROM background_tasks
                    LEFT JOIN sessions
                      ON sessions.workspace_id = background_tasks.workspace_id
                     AND sessions.session_id = background_tasks.session_id
                    WHERE background_tasks.workspace_id = ?
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
                    delegated_reminder_json = self._stop_delegated_reminder_state(
                        existing_payload=cast(str | None, row["delegated_reminder_json"]),
                        stop_condition="cancellation",
                        stopped_at_unix_ms=self._current_unix_ms(),
                    )
                    _ = connection.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'cancelled',
                            error = ?,
                            cancellation_cause = COALESCE(cancellation_cause, ?),
                            finished_at = ?,
                            updated_at = ?,
                            result_available = 0,
                            delegated_reminder_json = ?
                        WHERE workspace_id = ?
                          AND task_id = ?
                          AND status = 'running'
                          AND cancel_requested_at IS NOT NULL
                        """,
                        (
                            "cancelled by parent during delegated execution",
                            "cancelled by parent during delegated execution",
                            updated_at,
                            updated_at,
                            delegated_reminder_json,
                            str(workspace),
                            task_id,
                        ),
                    )
                else:
                    delegated_reminder_json = self._stop_delegated_reminder_state(
                        existing_payload=cast(str | None, row["delegated_reminder_json"]),
                        stop_condition="terminal_status",
                        stopped_at_unix_ms=self._current_unix_ms(),
                    )
                    _ = connection.execute(
                        """
                        UPDATE background_tasks
                        SET status = 'interrupted',
                            error = ?,
                            finished_at = ?,
                            updated_at = ?,
                            result_available = 1,
                            delegated_reminder_json = ?
                        WHERE workspace_id = ?
                          AND task_id = ?
                          AND status IN ('queued', 'running')
                          AND cancel_requested_at IS NULL
                        """,
                        (
                            message,
                            updated_at,
                            updated_at,
                            delegated_reminder_json,
                            str(workspace),
                            task_id,
                        ),
                    )
                reconciled_task_ids.append(task_id)
            connection.commit()
        return tuple(self.load_background_task(workspace=workspace, task_id=task_id) for task_id in reconciled_task_ids)

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
        with self._write_connect(workspace) as connection:
            current = self._background_task_runtime_row(
                connection=connection,
                workspace=workspace,
                task_id=task_id,
            )
            if is_background_task_terminal(self._parse_background_task_status(cast(str, current["status"]))):
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
                WHERE workspace_id = ? AND task_id = ?
                """,
                (
                    approval_request_id if approval_request_id is not None else cast(str | None, current["approval_request_id"]),
                    question_request_id if question_request_id is not None else cast(str | None, current["question_request_id"]),
                    cancellation_cause if cancellation_cause is not None else cast(str | None, current["cancellation_cause"]),
                    (1 if result_available else 0 if result_available is not None else cast(int, current["result_available"])),
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
        metadata = normalize_persisted_session_metadata(cast(dict[str, object], metadata))
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=cast(str, row["task_id"])),
            status=self._parse_background_task_status(cast(str, row["status"])),
            request=BackgroundTaskRequestSnapshot(
                prompt=cast(str, row["prompt"]),
                session_id=cast(str | None, row["request_session_id"]),
                parent_session_id=cast(str | None, row["request_parent_session_id"]),
                metadata=metadata,
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
            created_at_unix_ms=cast(int | None, row["created_at_unix_ms"]),
            started_at_unix_ms=cast(int | None, row["started_at_unix_ms"]),
            finished_at_unix_ms=cast(int | None, row["finished_at_unix_ms"]),
            cancel_requested_at=cast(int | None, row["cancel_requested_at"]),
            delegated_reminder=self._delegated_reminder_state_from_payload(cast(str | None, row["delegated_reminder_json"])),
            keep_alive=bool(cast(int, row["keep_alive"])),
            steer_prompt=cast(str | None, row["steer_prompt"]),
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
                WHERE workspace_id = ? AND task_id = ?
                """,
                (str(workspace), task_id),
            ).fetchone(),
        )
        if row is None:
            raise ValueError(f"unknown background task: {task_id}")
        return row

    def _next_background_task_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="background_tasks")

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
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            ).fetchone(),
        )
        return self._background_task_runtime_state_from_session_row(row)

    @staticmethod
    def _background_task_status_counts(*, connection: sqlite3.Connection, workspace: Path) -> dict[str, int]:
        return {
            cast(str, row["status"]): cast(int, row["count"])
            for row in cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM background_tasks
                    WHERE workspace_id = ?
                    GROUP BY status
                    ORDER BY status ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        }
