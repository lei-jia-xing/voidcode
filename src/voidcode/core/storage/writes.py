"""Write helpers shared by more than one storage domain.

These operations run inside a caller-held transaction and connection lock. They take
the connection explicitly so the session/event domain and the activation/state domain
use one implementation instead of each growing a private copy.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence

from ..events import EventType
from .records import (
    MAX_COMMITTED_EVENT_BYTES,
    EventDraft,
    SessionRecord,
    StagedStateUpdate,
    StateVersionConflict,
    StoreConflict,
    StoredEvent,
    _decode_session,
    canonical_json,
)


def set_window_projection_locked(
    connection: sqlite3.Connection,
    session_id: str,
    owner: str,
    *,
    covered_from_sequence: int,
    covered_to_sequence: int,
    replacement: str,
    covered_bytes: int,
    dropped_bytes: int,
) -> None:
    """Record one verified projection; the caller already verified the session and holds the transaction."""
    now = int(time.time() * 1000)
    connection.execute(
        """
        INSERT INTO window_projections (
            session_id, owner, covered_from_sequence, covered_to_sequence,
            replacement, covered_bytes, dropped_bytes, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (session_id) DO UPDATE SET
            owner = excluded.owner,
            covered_from_sequence = excluded.covered_from_sequence,
            covered_to_sequence = excluded.covered_to_sequence,
            replacement = excluded.replacement,
            covered_bytes = excluded.covered_bytes,
            dropped_bytes = excluded.dropped_bytes,
            created_at = excluded.created_at
        """,
        (
            session_id,
            owner,
            covered_from_sequence,
            covered_to_sequence,
            replacement,
            covered_bytes,
            dropped_bytes,
            now,
        ),
    )


def apply_state_updates_locked(
    connection: sqlite3.Connection,
    state_updates: Sequence[StagedStateUpdate],
    *,
    default_revision_id: str,
    now: int,
) -> None:
    """Apply owner state copies with their optimistic-concurrency obligation, inside an open transaction."""
    seen: set[tuple[str, str, str]] = set()
    for st in state_updates:
        key = (st.scope, st.scope_key, st.owner)
        if key in seen:
            raise StoreConflict(f"duplicate state update for {st.owner!r} in {st.scope}:{st.scope_key} in one commit")
        seen.add(key)
        payload_json = canonical_json(st.payload)
        actual_rev_id = st.revision_id or default_revision_id
        if st.expected_version == 0:
            try:
                connection.execute(
                    """
                    INSERT INTO plugin_state (scope, scope_key, owner, version, schema_version, revision_id, payload_json, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (st.scope, st.scope_key, st.owner, st.schema_version, actual_rev_id, payload_json, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StateVersionConflict(f"state already exists for owner {st.owner!r} in {st.scope}:{st.scope_key}") from exc
        else:
            c = connection.execute(
                """
                UPDATE plugin_state
                SET version = version + 1, schema_version = ?, revision_id = ?, payload_json = ?, updated_at = ?
                WHERE scope = ? AND scope_key = ? AND owner = ? AND version = ?
                """,
                (st.schema_version, actual_rev_id, payload_json, now, st.scope, st.scope_key, st.owner, st.expected_version),
            )
            if c.rowcount == 0:
                raise StateVersionConflict(
                    f"state CAS conflict for {st.owner!r} in {st.scope}:{st.scope_key}: expected version {st.expected_version}"
                )


def read_session(connection: sqlite3.Connection, session_id: str) -> SessionRecord:
    """Read one session row inside the caller's transaction; unknown ids raise like `SqliteStore.session`."""
    row = connection.execute(
        """
        SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
               parent_session_id, root_session_id, depth, tool_allowlist_json
        FROM sessions
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown session: {session_id}")
    return _decode_session(row)


def record_event_indexes(connection: sqlite3.Connection, session_id: str, event_type: str, payload: Mapping[str, object], now: int) -> None:
    """Mirror indexable event keys into side tables; callers hold the lock and a transaction."""
    if event_type == EventType.ASK_ANSWERED:
        request_id = payload.get("request_id")
        if isinstance(request_id, str) and request_id:
            connection.execute(
                "INSERT OR IGNORE INTO ask_answers (session_id, request_id, created_at) VALUES (?, ?, ?)",
                (session_id, request_id, now),
            )
    elif event_type == EventType.CONTINUATION_STARTED:
        continuation_id = payload.get("continuation_id")
        if isinstance(continuation_id, str) and continuation_id:
            connection.execute(
                "INSERT OR IGNORE INTO continuation_starts (session_id, continuation_id, created_at) VALUES (?, ?, ?)",
                (session_id, continuation_id, now),
            )


def insert_session_event_locked(
    connection: sqlite3.Connection,
    session_id: str,
    sequence: int,
    *,
    run_id: str,
    revision_id: str,
    draft: EventDraft,
    now: int,
) -> StoredEvent:
    """Append one session event row and its side indexes inside an open transaction."""
    payload_json = canonical_json(draft.payload)
    payload_size = len(payload_json.encode("utf-8"))
    if payload_size > MAX_COMMITTED_EVENT_BYTES:
        raise StoreConflict(
            f"session event {draft.event_type!r} payload is {payload_size} bytes, "
            f"exceeding the {MAX_COMMITTED_EVENT_BYTES}-byte committed-event bound"
        )
    connection.execute(
        """
        INSERT INTO session_events (
            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            sequence,
            run_id,
            draft.event_type,
            draft.source,
            draft.actor,
            revision_id,
            payload_json,
            payload_size,
        ),
    )
    record_event_indexes(connection, session_id, draft.event_type, draft.payload, now)
    return StoredEvent(
        session_id=session_id,
        sequence=sequence,
        run_id=run_id,
        event_type=draft.event_type,
        source=draft.source,
        actor=draft.actor,
        revision_id=revision_id,
        payload=draft.payload,
    )
