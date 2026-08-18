from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .contracts import (
    RuntimeSessionRevertMarker,
    UnknownSessionError,
)
from .events import EventEnvelope
from .session import normalize_persisted_session_metadata
from .session_metadata_helpers import session_metadata_with_runtime_state_updates

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _RevertStorageMixin(_MixinBase):
    @staticmethod
    def _revert_marker_from_metadata(
        metadata: dict[str, object],
    ) -> RuntimeSessionRevertMarker | None:
        raw_marker = metadata.get("conversation_revert")
        if not isinstance(raw_marker, dict):
            return None
        marker_payload = cast(dict[object, object], raw_marker)
        raw_sequence = marker_payload.get("sequence")
        if not isinstance(raw_sequence, int) or isinstance(raw_sequence, bool) or raw_sequence < 1:
            return None
        raw_active = marker_payload.get("active", True)
        active = raw_active if isinstance(raw_active, bool) else True
        return RuntimeSessionRevertMarker(sequence=raw_sequence, active=active)

    @staticmethod
    def _metadata_with_revert_marker(
        metadata: dict[str, object],
        marker: RuntimeSessionRevertMarker | None,
    ) -> dict[str, object]:
        next_metadata = dict(metadata)
        if marker is None:
            next_metadata.pop("conversation_revert", None)
            return next_metadata
        next_metadata["conversation_revert"] = {
            "sequence": marker.sequence,
            "active": marker.active,
        }
        return next_metadata

    def _active_revert_metadata(
        self,
        metadata: dict[str, object],
        *,
        events: tuple[EventEnvelope, ...],
    ) -> dict[str, object]:
        next_metadata = dict(metadata)
        raw_runtime_state = next_metadata.get("runtime_state")
        if not isinstance(raw_runtime_state, dict):
            return next_metadata
        todo_state = self._todo_state_from_events(events)
        if todo_state is None:
            removed = frozenset({"context_projection", "todos"})
            updates = None
        else:
            removed = frozenset({"context_projection"})
            updates = {"todos": todo_state}
        return session_metadata_with_runtime_state_updates(
            next_metadata,
            updates=updates,
            removed=removed,
        )

    def _session_metadata_and_events(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
    ) -> tuple[dict[str, object], tuple[EventEnvelope, ...]]:
        session_row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT metadata_json
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
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
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY sequence ASC
                """,
                (str(workspace), session_id),
            ).fetchall(),
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
        return (
            normalize_persisted_session_metadata(cast(dict[str, object], json.loads(cast(str, session_row["metadata_json"])))),
            events,
        )

    def _write_revert_marker(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
        marker: RuntimeSessionRevertMarker | None,
    ) -> None:
        metadata, _events = self._session_metadata_and_events(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        updated_at = self._next_timestamp(connection=connection)
        _ = connection.execute(
            """
            UPDATE sessions
            SET metadata_json = ?, updated_at = ?
            WHERE workspace_id = ? AND session_id = ?
            """,
            (
                json.dumps(self._metadata_with_revert_marker(metadata, marker), sort_keys=True),
                updated_at,
                str(workspace),
                session_id,
            ),
        )

    def revert_session(self, *, workspace: Path, session_id: str, sequence: int) -> RuntimeSessionRevertMarker:
        if sequence < 1:
            raise ValueError("revert sequence must be a positive integer")
        with self._write_connect(workspace) as connection:
            _metadata, events = self._session_metadata_and_events(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            if not any(event.sequence == sequence for event in events):
                raise ValueError(f"session {session_id} has no event sequence {sequence}")
            marker = RuntimeSessionRevertMarker(sequence=sequence, active=True)
            self._write_revert_marker(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                marker=marker,
            )
            connection.commit()
            return marker

    def undo_session(self, *, workspace: Path, session_id: str) -> RuntimeSessionRevertMarker:
        with self._write_connect(workspace) as connection:
            metadata, events = self._session_metadata_and_events(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            active_marker = self._revert_marker_from_metadata(metadata)
            active_cutoff = active_marker.sequence if active_marker is not None else None
            candidate = next(
                (
                    event
                    for event in reversed(events)
                    if event.event_type == "runtime.request_received" and (active_cutoff is None or event.sequence < active_cutoff)
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"session {session_id} has no user turn to undo")
            marker = RuntimeSessionRevertMarker(sequence=candidate.sequence, active=True)
            self._write_revert_marker(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                marker=marker,
            )
            connection.commit()
            return marker

    def unrevert_session(self, *, workspace: Path, session_id: str) -> RuntimeSessionRevertMarker | None:
        with self._write_connect(workspace) as connection:
            metadata, _events = self._session_metadata_and_events(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            marker = self._revert_marker_from_metadata(metadata)
            if marker is None:
                connection.commit()
                return None
            self._write_revert_marker(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                marker=None,
            )
            connection.commit()
            return marker
