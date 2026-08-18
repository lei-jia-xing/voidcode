from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .events import (
    RUNTIME_TODO_UPDATED,
    EventEnvelope,
)
from .session_metadata_helpers import (
    runtime_state_todos,
    session_metadata_with_runtime_state_updates,
)
from .todos import (
    runtime_todos_from_state_payload,
    todo_state_payload,
)

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _TodoStorageMixin(_MixinBase):
    @staticmethod
    def _todo_state_from_metadata(metadata: dict[str, object]) -> dict[str, object] | None:
        todo_state = runtime_state_todos(metadata)
        if todo_state is None:
            return None
        revision = todo_state.get("revision")
        return todo_state_payload(
            runtime_todos_from_state_payload(todo_state.get("todos")),
            revision=revision if isinstance(revision, int) and revision >= 0 else 0,
        )

    def _replace_session_todos(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
        metadata: dict[str, object],
    ) -> None:
        todo_state = self._todo_state_from_metadata(metadata)
        _ = connection.execute(
            "DELETE FROM session_todos WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if todo_state is None:
            return
        todos = runtime_todos_from_state_payload(todo_state.get("todos"))
        _ = connection.executemany(
            """
            INSERT INTO session_todos (
                workspace_id, session_id, position, content, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(workspace),
                    session_id,
                    todo["position"],
                    todo["content"],
                    todo["status"],
                    todo["updated_at"],
                )
                for todo in todos
            ],
        )

    def _todo_state_from_rows(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
    ) -> dict[str, object] | None:
        rows = cast(
            list[sqlite3.Row],
            connection.execute(
                """
                SELECT position, content, status, updated_at
                FROM session_todos
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY position ASC
                """,
                (str(workspace), session_id),
            ).fetchall(),
        )
        if not rows:
            return None
        todos = runtime_todos_from_state_payload(
            [
                {
                    "position": cast(int, row["position"]),
                    "content": cast(str, row["content"]),
                    "status": cast(str, row["status"]),
                    "updated_at": cast(int, row["updated_at"]),
                }
                for row in rows
            ]
        )
        revision = max((todo["updated_at"] for todo in todos), default=0)
        return todo_state_payload(todos, revision=revision)

    @classmethod
    def _metadata_with_todo_state(
        cls,
        metadata: dict[str, object],
        todo_state: dict[str, object] | None,
    ) -> dict[str, object]:
        if todo_state is None:
            return metadata
        return session_metadata_with_runtime_state_updates(
            metadata,
            updates={"todos": todo_state},
        )

    @staticmethod
    def _todo_state_from_events(events: tuple[EventEnvelope, ...]) -> dict[str, object] | None:
        for event in reversed(events):
            if event.event_type != RUNTIME_TODO_UPDATED:
                continue
            todos = runtime_todos_from_state_payload(event.payload.get("todos"))
            revision = event.payload.get("revision")
            return todo_state_payload(
                todos,
                revision=revision if isinstance(revision, int) and revision >= 0 else 0,
            )
        return None
