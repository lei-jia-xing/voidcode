from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .contracts import (
    RuntimeNotification,
    RuntimeNotificationKind,
    RuntimeNotificationStatus,
    RuntimeRequest,
    RuntimeResponse,
)
from .permission import PendingApproval
from .question import PendingQuestion
from .session import SessionRef

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _NotificationStorageMixin(_MixinBase):
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
                                      AND sessions.workspace_id = notifications.workspace_id
                    WHERE notifications.workspace_id = ?
                    ORDER BY notifications.created_at DESC, notifications.notification_id DESC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._notification_from_row(row) for row in rows)

    def acknowledge_notification(self, *, workspace: Path, notification_id: str) -> RuntimeNotification:
        with self._write_connect(workspace) as connection:
            existing_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT notification_id, session_id, kind, status, summary, payload_json,
                           event_sequence, created_at, acknowledged_at
                    FROM session_notifications
                    WHERE workspace_id = ? AND notification_id = ?
                    """,
                    (str(workspace), notification_id),
                ).fetchone(),
            )
            if existing_row is None:
                raise ValueError(f"unknown notification: {notification_id}")
            acknowledged_at = cast(int | None, existing_row["acknowledged_at"])
            if acknowledged_at is None:
                acknowledged_at = self._next_auxiliary_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    UPDATE session_notifications
                    SET status = 'acknowledged', acknowledged_at = ?
                    WHERE workspace_id = ? AND notification_id = ?
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
                                      AND sessions.workspace_id = notifications.workspace_id
                    WHERE notifications.workspace_id = ? AND notifications.notification_id = ?
                    """,
                    (str(workspace), notification_id),
                ).fetchone(),
            )
        return self._notification_from_row(row)

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
        last_event_sequence: int | None = None,
    ) -> None:
        session_id = response.session.session.id
        if last_event_sequence is None:
            # Callers that do not pass the durable watermark (none in-tree) fall
            # back to the persisted event-log max so notification references can
            # never point at a phantom (locally-resequenced) sequence.
            last_event_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
        notification = self._notification_candidate(
            request=request,
            response=response,
            pending_approval=pending_approval,
            pending_question=pending_question,
            notification_run_id=notification_run_id,
            last_event_sequence=last_event_sequence,
        )
        if pending_approval is None:
            _ = connection.execute(
                """
                UPDATE session_notifications
                SET status = 'acknowledged',
                    acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'approval_blocked'
                  AND status = 'unread'
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
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
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'approval_blocked'
                  AND status = 'unread'
                  AND notification_id != ?
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
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
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'question_blocked'
                  AND status = 'unread'
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
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
                WHERE workspace_id = ?
                  AND session_id = ?
                  AND kind = 'question_blocked'
                  AND status = 'unread'
                  AND notification_id != ?
                """,
                (
                    self._next_auxiliary_timestamp(connection=connection),
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
                notification_id, workspace_id, session_id, kind, status, summary, payload_json,
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
                self._next_auxiliary_timestamp(connection=connection),
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
        last_event_sequence: int | None = None,
    ) -> dict[str, object] | None:
        if pending_approval is not None:
            return self._approval_notification_candidate(
                request=request,
                response=response,
                pending_approval=pending_approval,
                last_event_sequence=last_event_sequence,
            )
        if pending_question is not None:
            return self._question_notification_candidate(
                request=request,
                response=response,
                pending_question=pending_question,
                last_event_sequence=last_event_sequence,
            )
        return self._terminal_notification_candidate(
            request=request,
            response=response,
            notification_run_id=notification_run_id,
            last_event_sequence=last_event_sequence,
        )

    def _approval_notification_candidate(
        self,
        *,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval: PendingApproval,
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        session_id = response.session.session.id
        summary, _ = self._result_summary(response=response, prompt=request.prompt)
        event_sequence = self._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence
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
        last_event_sequence: int | None = None,
    ) -> dict[str, object]:
        session_id = response.session.session.id
        summary, _ = self._result_summary(response=response, prompt=request.prompt)
        event_sequence = self._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence
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
                        "options": [{"label": option.label, "description": option.description} for option in prompt.options],
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
        last_event_sequence: int | None = None,
    ) -> dict[str, object] | None:
        session_id = response.session.session.id
        event_sequence = self._session_last_event_sequence(response.events) if last_event_sequence is None else last_event_sequence
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

    def _read_created_at(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT created_at FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is not None:
            return cast(int, row["created_at"])
        return self._next_auxiliary_timestamp(connection=connection)

    def _read_created_at_unix_ms(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int | None:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT created_at_unix_ms FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is not None and row["created_at_unix_ms"] is not None:
            return cast(int, row["created_at_unix_ms"])
        return None

    @staticmethod
    def _read_last_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT last_event_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            ).fetchone(),
        )
        if row is not None:
            return cast(int, row["last_event_sequence"])
        return 0

    @staticmethod
    def _max_persisted_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM session_events WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])
