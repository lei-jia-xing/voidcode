from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from ..events import RUNTIME_TODO_UPDATED, EventEnvelope
from ..session_metadata_helpers import runtime_state_todos, session_metadata_with_runtime_state_updates
from ..todos import runtime_todo_phases_from_payload, todo_state_payload

if TYPE_CHECKING:
    from .shared import _StorageMixinBase

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
        if not isinstance(revision, int) or revision < 0:
            raise ValueError("runtime todo revision must be a non-negative integer")
        return todo_state_payload(runtime_todo_phases_from_payload(todo_state.get("phases")), revision=revision)

    def _replace_session_todos(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
        metadata: dict[str, object],
    ) -> None:
        # Todo truth is persisted in runtime metadata and append-only events.
        _ = self, connection, workspace, session_id, metadata

    def _todo_state_from_rows(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
    ) -> dict[str, object] | None:
        # Todo truth is persisted in runtime metadata and append-only events.
        _ = self, connection, workspace, session_id
        return None

    @classmethod
    def _metadata_with_todo_state(cls, metadata: dict[str, object], todo_state: dict[str, object] | None) -> dict[str, object]:
        if todo_state is None:
            return metadata
        return session_metadata_with_runtime_state_updates(metadata, updates={"todos": todo_state})

    @staticmethod
    def _todo_state_from_events(events: tuple[EventEnvelope, ...]) -> dict[str, object] | None:
        for event in reversed(events):
            if event.event_type != RUNTIME_TODO_UPDATED:
                continue
            phases = runtime_todo_phases_from_payload(event.payload.get("phases"))
            revision = event.payload.get("revision")
            if not isinstance(revision, int) or revision < 0:
                raise ValueError("runtime todo revision must be a non-negative integer")
            return todo_state_payload(phases, revision=revision)
        return None
