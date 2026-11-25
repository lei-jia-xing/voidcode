"""Append-only event stream and checkpoint readers.

Bounded/typed reads over `session_events` and `checkpoints`, plus the live ask-park scan
the expiry sweep consumes. Appends and checkpoint writes belong to `commits`, `writes`,
and the domain transactions that own their atomic boundaries.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`, and session existence checks
go through the session mixin's `session`.
"""

from __future__ import annotations

from collections.abc import Sequence

from ._database import _ConnectionStore
from ._rows import _decode_checkpoint, _decode_event, _decode_waiting_ask
from ._values import Checkpoint, StoredEvent, WaitingAskPark


class _EventStreamMixin(_ConnectionStore):
    """Event-stream and checkpoint reads over the store's immutable append log."""

    def events(self, session_id: str, after_sequence: int = 0, limit: int | None = None) -> tuple[StoredEvent, ...]:
        """Fetch stored events for a session strictly after a given sequence number, optionally limited."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            if limit is not None:
                cursor = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    LIMIT ?
                    """,
                    (session_id, after_sequence, max(1, limit)),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    """,
                    (session_id, after_sequence),
                )
            return tuple(_decode_event(row) for row in cursor.fetchall())

    def events_after(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[StoredEvent, ...]:
        """Bounded tail read of stored events for continuous SSE streaming."""
        return self.events(session_id, after_sequence=after_sequence, limit=limit)

    def latest_event(self, session_id: str, event_type: str, run_id: str | None = None) -> StoredEvent | None:
        """Newest event of one type (optionally one run), or None when absent."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            if run_id is None:
                row = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND event_type = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (session_id, event_type),
                ).fetchone()
            else:
                row = self._connection.execute(
                    """
                    SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                    FROM session_events
                    WHERE session_id = ? AND event_type = ? AND run_id = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (session_id, event_type, run_id),
                ).fetchone()
            if row is None:
                return None
            return _decode_event(row)

    def events_by_types(self, session_id: str, types: Sequence[str], after_sequence: int = 0, limit: int | None = None) -> tuple[StoredEvent, ...]:
        """Events of the given types in ascending sequence order, optionally ranged."""
        if not types:
            return ()
        with self._lock:
            self._check_open()
            self.session(session_id)
            placeholders = ", ".join("?" for _ in types)
            sql = f"""
                SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                FROM session_events
                WHERE session_id = ? AND event_type IN ({placeholders}) AND sequence > ?
                ORDER BY sequence ASC
                """
            params: list[object] = [session_id, *types, after_sequence]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(max(1, limit))
            cursor = self._connection.execute(sql, tuple(params))
            return tuple(_decode_event(row) for row in cursor.fetchall())

    def event_payload_bytes(self, session_id: str, after_sequence: int = 0, limit: int | None = None) -> dict[int, int]:
        """Stored canonical payload bytes by sequence for a range; no payload decode."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            if limit is not None:
                cursor = self._connection.execute(
                    """
                    SELECT sequence, payload_bytes
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    LIMIT ?
                    """,
                    (session_id, after_sequence, max(1, limit)),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT sequence, payload_bytes
                    FROM session_events
                    WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    """,
                    (session_id, after_sequence),
                )
            return {sequence: payload_bytes for (sequence, payload_bytes) in cursor.fetchall()}

    def event_type_counts(self, session_id: str) -> dict[str, int]:
        """Count committed events by type with one SQL aggregate, no payload reads."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            cursor = self._connection.execute(
                """
                SELECT event_type, COUNT(*)
                FROM session_events
                WHERE session_id = ?
                GROUP BY event_type
                """,
                (session_id,),
            )
            return {event_type: count for (event_type, count) in cursor.fetchall()}

    def checkpoint(self, session_id: str) -> Checkpoint | None:
        """Fetch the current checkpoint of a session, or None if none has been committed."""
        with self._lock:
            self._check_open()
            # Verify session exists
            self.session(session_id)
            cursor = self._connection.execute(
                """
                SELECT kind, revision_id, run_id, last_event_sequence, payload_json
                FROM checkpoints
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _decode_checkpoint(row)

    def waiting_ask_parks(self) -> tuple[WaitingAskPark, ...]:
        """Waiting sessions holding a live ask binding, from checkpoint columns only.

        Candidates for the ask-expiry sweep: no event scan, no answer resolution.
        The caller revalidates each candidate (session, binding, request, answer
        absence) under that session's lock before settling anything.
        """
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT s.session_id, c.run_id, c.revision_id,
                       c.ask_sequence,
                       c.ask_request_id,
                       c.last_event_sequence
                FROM sessions s JOIN checkpoints c ON c.session_id = s.session_id
                WHERE s.status = 'waiting' AND c.run_id = s.run_id
                  AND c.kind <> 'terminal'
                  AND c.ask_request_id IS NOT NULL
                ORDER BY s.session_id ASC
                """
            ).fetchall()
            return tuple(_decode_waiting_ask(row) for row in rows)
