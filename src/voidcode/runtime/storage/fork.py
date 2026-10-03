"""Copy retained prefix rows into a new session without rewriting parent edges.

The child owns its own leaf/watermark and separate fork provenance. Delegated
``parent_session_id`` is never fork provenance. Pair safety follows the selected
ancestor path; abandoned rows remain append-only but cannot close its calls.
Session config/policy transfer; position-scoped runtime state is discarded.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from ..contracts import (
    RuntimeSessionForkBoundaryError,
    SessionLineageCycleError,
    UnknownSessionError,
)
from ..events import (
    RUNTIME_APPROVAL_REQUESTED,
    RUNTIME_APPROVAL_RESOLVED,
    RUNTIME_QUESTION_ANSWERED,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_TOOL_COMPLETED,
    RUNTIME_TOOL_STARTED,
    EventEnvelope,
)
from ..session import (
    SessionRef,
    SessionStatus,
    StoredSessionForestEntry,
    StoredSessionLineageEntry,
    StoredSessionSummary,
    normalize_persisted_session_metadata,
)
from .rows import (
    SessionEventPrefixRow,
    SessionForkSourceRow,
    SessionLineageRow,
    decode_row,
    fetch_rows,
)

if TYPE_CHECKING:
    from .shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object

# A normalized graph request and governed runtime start describe the same native
# identity, even when input governance rewrites its name/arguments.
_TOOL_REQUEST_TYPES = frozenset({"graph.tool_request_created", RUNTIME_TOOL_STARTED})
_REQUEST_PAIRS = (
    (RUNTIME_APPROVAL_REQUESTED, RUNTIME_APPROVAL_RESOLVED),
    (RUNTIME_QUESTION_REQUESTED, RUNTIME_QUESTION_ANSWERED),
)

#: Metadata keys that describe a *position in a run* rather than a session
#: identity. A fork replays the copied events instead of resuming them, so an
#: inherited position would point at sequences the fork does not own. Only
#: ``runtime_state`` (context projection + todos) is position-scoped; the
#: session-scoped effective config and policy must carry over or the fork is
#: unusable (``run -r``/inspection require ``runtime_config``).
_NON_TRANSFERABLE_METADATA_KEYS = ("runtime_state",)


def _normalized_tool_call_id(payload: dict[str, object]) -> str | None:
    raw = payload.get("tool_call_id")
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _dangling_interaction(events: tuple[EventEnvelope, ...]) -> tuple[str, str] | None:
    """Return ``(kind, label)`` for an interaction the prefix leaves open.

    ``kind`` is ``"tool"`` or ``"request"`` and ``label`` the offending id. A
    tool call whose ``tool_call_id`` has no matching completion, or an
    approval/question request whose ``request_id`` has no resolution, means the
    boundary splits a pair.
    """
    open_tools: dict[str, None] = {}
    open_requests: dict[tuple[str, str], None] = {}
    for event in events:
        payload = event.payload
        if event.event_type in _TOOL_REQUEST_TYPES or event.event_type == RUNTIME_TOOL_COMPLETED:
            call_id = _normalized_tool_call_id(payload)
            if call_id is None:
                return ("tool", f"event {event.sequence} lacks an original native identity; migrate the legacy history")
            if event.event_type in _TOOL_REQUEST_TYPES:
                open_tools[call_id] = None
            else:
                open_tools.pop(call_id, None)
            continue
        for request_type, resolution_type in _REQUEST_PAIRS:
            if event.event_type not in {request_type, resolution_type}:
                continue
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                return ("request", f"event {event.sequence} lacks its original request identity")
            key = (request_type, request_id)
            if event.event_type == request_type:
                open_requests[key] = None
            else:
                open_requests.pop(key, None)
    if open_tools:
        return ("tool", next(reversed(open_tools)))
    if open_requests:
        request_type, request_id = next(iter(open_requests))
        return ("request", f"{request_id} ({request_type})")
    return None


def forest_from_lineage_entries(
    entries: tuple[StoredSessionLineageEntry, ...],
) -> tuple[StoredSessionForestEntry, ...]:
    """Lay out the whole fork forest: display order, parents before children.

    This is the ONE ordering rule every tree surface renders (CLI ``sessions
    tree``, TUI resume picker, web sidebar); no client re-sorts. Structure and
    display order are the same projection on purpose — a second, client-side
    sort is exactly what put children above their parents.

    * **Roots are recency-ordered**, ``updated_at DESC`` (``session_id ASC``
      breaking ties): most recently active session first, which is what a user
      picking a session expects.
    * **Children follow their parent** and siblings keep the forest's
      deterministic ``(forked_at_sequence ASC NULLS FIRST, session_id ASC)``
      order, derived from provenance rather than wall-clock. A forking parent
      continued *after* its forks has the newer ``updated_at``; ordering by it
      at every level would put the forks above the parent and collapse their
      depth. Depth is therefore topological, one more than the parent's.

    A row is a root when it has no provenance or when its parent is absent from
    ``entries`` (a fork of a deleted/disabled session stays visible as a root).
    Raises :class:`SessionLineageCycleError` when provenance contains a cycle:
    such rows have no root, so silently dropping them would hide corruption and
    a naive walk would never terminate.
    """
    children: dict[str, list[StoredSessionLineageEntry]] = {}
    present = {entry.session_id for entry in entries}
    roots: list[StoredSessionLineageEntry] = []
    for entry in entries:
        parent = entry.forked_from_session_id
        if parent is None or parent not in present:
            roots.append(entry)
        else:
            children.setdefault(parent, []).append(entry)

    def _sibling_order(entry: StoredSessionLineageEntry) -> tuple[int, str]:
        sequence = entry.forked_at_sequence
        return (-1 if sequence is None else sequence, entry.session_id)

    # Display order for roots: ``updated_at`` descending, ``session_id``
    # ascending to break ties. The LIFO walk below pops in push order, so the
    # root list is pushed reversed to emit the newest root first.
    roots.sort(key=lambda entry: (-entry.updated_at, entry.session_id))
    for siblings in children.values():
        siblings.sort(key=_sibling_order)

    forest: list[StoredSessionForestEntry] = []
    placed: set[str] = set()
    stack: list[tuple[int, StoredSessionLineageEntry]] = [(0, root) for root in reversed(roots)]
    while stack:
        depth, entry = stack.pop()
        placed.add(entry.session_id)
        forest.append(
            StoredSessionForestEntry(
                session_id=entry.session_id,
                forked_from_session_id=entry.forked_from_session_id,
                forked_at_sequence=entry.forked_at_sequence,
                depth=depth,
            )
        )
        stack.extend((depth + 1, child) for child in reversed(children.get(entry.session_id, ())))
    if len(placed) != len(entries):
        unplaced = sorted(entry.session_id for entry in entries if entry.session_id not in placed)
        raise SessionLineageCycleError(f"fork provenance contains a cycle: {', '.join(unplaced)}")
    return tuple(forest)


class _ForkStorageMixin(_MixinBase):
    def fork_session(
        self,
        *,
        workspace: Path,
        session_id: str,
        at_sequence: int | None = None,
    ) -> StoredSessionSummary:
        """Copy ``session_id``'s events ``1..N`` into a brand-new session row.

        ``N`` defaults to the source's own watermark. The copied events keep
        their original sequence numbers (so any copied revert marker still
        resolves), but the fork's ``last_event_sequence`` is its own ``N``, never
        the source's. The whole copy is one ``BEGIN IMMEDIATE`` transaction and
        the source row/log is read-only here — nothing about the source changes.

        A boundary that splits a tool call from its result, or an approval/
        question request from its resolution, is refused: the fork would replay
        an interaction the log never closes.
        """
        with self._write_connect(workspace) as connection:
            source = fetch_rows(
                connection,
                """
                SELECT session_id, parent_session_id, status, turn, prompt, title,
                       metadata_json, last_event_sequence
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            )
            if not source:
                raise UnknownSessionError(f"unknown session: {session_id}")
            source_row = decode_row(source[0], SessionForkSourceRow)
            watermark = int(source_row["last_event_sequence"])
            if at_sequence is None:
                boundary = watermark
            else:
                if at_sequence < 1:
                    raise ValueError("fork sequence must be a positive integer")
                boundary = min(at_sequence, watermark)
            event_rows = fetch_rows(
                connection,
                """
                SELECT sequence, parent_sequence, event_type, source, payload_json
                FROM session_events
                WHERE workspace_id = ? AND session_id = ? AND sequence <= ?
                ORDER BY sequence ASC
                """,
                (str(workspace), session_id, boundary),
            )
            decoded_rows = tuple(decode_row(row, SessionEventPrefixRow) for row in event_rows)
            events = tuple(
                EventEnvelope(
                    session_id=session_id,
                    sequence=row["sequence"],
                    event_type=row["event_type"],
                    source=self._parse_event_source(row["source"]),
                    payload=json.loads(row["payload_json"]),
                )
                for row in decoded_rows
            )
            if not events:
                raise ValueError(f"session {session_id} has no events at or before sequence {boundary} to fork")
            from .sessions import SessionTreeEvent, session_event_path

            path = session_event_path(
                tuple(SessionTreeEvent(event, row["parent_sequence"]) for event, row in zip(events, decoded_rows, strict=True)),
                target_sequence=boundary,
            )
            dangling = _dangling_interaction(path)
            if dangling is not None:
                kind, label = dangling
                safe_sequence = events[-1].sequence
                raise RuntimeSessionForkBoundaryError(
                    f"fork boundary {boundary} splits a {kind} call from its result ({label}); fork at or before sequence {safe_sequence} instead"
                )
            forked_id = f"session-{uuid4().hex}"
            updated_at = self._next_timestamp(connection=connection)
            created_at = self._read_created_at(
                connection=connection,
                workspace=workspace,
                session_id=forked_id,
            )
            created_at_unix_ms = self._current_unix_ms()
            forked_metadata = _fork_metadata(
                raw_metadata_json=source_row["metadata_json"],
                workspace=str(workspace),
            )
            try:
                _ = connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, parent_session_id, workspace_id, status, turn, prompt, title, output,
                        metadata_json, pending_approval_json, pending_question_json,
                        resume_checkpoint_json, created_at, updated_at,
                        last_event_sequence, leaf_sequence, created_at_unix_ms,
                        forked_from_session_id, forked_at_sequence
                    ) VALUES (?, NULL, ?, 'interrupted', ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        forked_id,
                        str(workspace),
                        source_row["turn"],
                        source_row["prompt"],
                        source_row["title"],
                        json.dumps(forked_metadata, sort_keys=True),
                        json.dumps(_fork_resume_checkpoint(boundary), sort_keys=True),
                        created_at,
                        updated_at,
                        boundary,
                        # The copied prefix's newest row is the fork's position,
                        # exactly as an append would leave it (rule 4).
                        boundary,
                        created_at_unix_ms,
                        session_id,
                        boundary,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO session_events (
                        workspace_id, session_id, sequence, parent_sequence, event_type, source, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(workspace),
                            forked_id,
                            row["sequence"],
                            # The prefix copy keeps each row's parent verbatim,
                            # so the copied chain is identical to the source.
                            row["parent_sequence"],
                            row["event_type"],
                            row["source"],
                            row["payload_json"],
                        )
                        for row in decoded_rows
                    ],
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - uuid collision only
                raise ValueError(f"fork target session id collided: {forked_id}") from exc
            connection.commit()
        return StoredSessionSummary(
            session=SessionRef(id=forked_id, parent_id=None),
            status=cast(SessionStatus, "interrupted"),
            turn=int(source_row["turn"]),
            prompt=str(source_row["prompt"]),
            updated_at=updated_at,
            title=source_row["title"],
            forked_from_session_id=session_id,
            forked_at_sequence=boundary,
        )

    def session_lineage(
        self,
        *,
        workspace: Path,
        session_id: str | None = None,
    ) -> tuple[StoredSessionLineageEntry, ...]:
        """Read-only walk of fork ancestry, oldest ancestor first, fork last.

        With ``session_id`` the walk starts there and follows
        ``forked_from_session_id`` upward. Without it, every workspace session
        participating in fork provenance is returned so a caller can layout the
        whole forest. Delegated background-task children (``parent_session_id``
        set) are excluded there: they are not fork nodes and belong only to the
        child-session view, matching the ``list_sessions`` filter every other
        surface applies. Disabled/deleted ancestors simply stop the walk.

        ``updated_at`` rides along because the forest orders *roots* by recency;
        the walk's own order here is provenance-only.
        """
        with self._connect(workspace) as connection:
            if session_id is None:
                rows = fetch_rows(
                    connection,
                    """
                    SELECT session_id, forked_from_session_id, forked_at_sequence, updated_at
                    FROM sessions
                    WHERE workspace_id = ? AND parent_session_id IS NULL
                    ORDER BY updated_at ASC, session_id ASC
                    """,
                    (str(workspace),),
                )
                return tuple(
                    StoredSessionLineageEntry(
                        session_id=row["session_id"],
                        forked_from_session_id=row["forked_from_session_id"],
                        forked_at_sequence=row["forked_at_sequence"],
                        updated_at=row["updated_at"],
                    )
                    for row in (decode_row(row, SessionLineageRow) for row in rows)
                )
            chain: list[StoredSessionLineageEntry] = []
            seen: set[str] = set()
            cursor: str | None = session_id
            while cursor is not None and cursor not in seen:
                seen.add(cursor)
                row = fetch_rows(
                    connection,
                    """
                    SELECT session_id, forked_from_session_id, forked_at_sequence, updated_at
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), cursor),
                )
                if not row:
                    if cursor == session_id:
                        raise UnknownSessionError(f"unknown session: {session_id}")
                    break
                entry_row = decode_row(row[0], SessionLineageRow)
                chain.append(
                    StoredSessionLineageEntry(
                        session_id=entry_row["session_id"],
                        forked_from_session_id=entry_row["forked_from_session_id"],
                        forked_at_sequence=entry_row["forked_at_sequence"],
                        updated_at=entry_row["updated_at"],
                    )
                )
                cursor = entry_row["forked_from_session_id"]
        chain.reverse()
        return tuple(chain)

    def session_forest(self, *, workspace: Path) -> tuple[StoredSessionForestEntry, ...]:
        """Lay out every workspace session's fork forest, parents before children.

        Storage supplies the flat provenance rows (``session_lineage`` without a
        ``session_id``); :func:`forest_from_lineage_entries` owns the tree
        layout so the CLI, TUI picker, and HTTP surface all indent the same
        forest instead of each re-deriving depth from rows that are merely
        ``updated_at``-ordered.
        """
        return forest_from_lineage_entries(self.session_lineage(workspace=workspace))


def _fork_metadata(
    *,
    raw_metadata_json: str,
    workspace: str,
) -> dict[str, object]:
    """Metadata for the fork: identity carried over, run position dropped.

    No ``conversation_revert`` key is carried: that marker is gone with the
    linear revert mechanism (position now lives in ``leaf_sequence``), so a
    fork inherits a position by copying the prefix, not a marker.
    """
    source_metadata = normalize_persisted_session_metadata(cast(dict[str, object], json.loads(raw_metadata_json)))
    forked = {key: value for key, value in source_metadata.items() if key not in _NON_TRANSFERABLE_METADATA_KEYS}
    forked["workspace"] = workspace
    return forked


def _fork_resume_checkpoint(boundary: int) -> dict[str, object]:
    """Replay-only checkpoint for the fork's own watermark.

    ``kind="terminal"`` makes ``resume`` take the stored-replay path instead of
    truncating and re-running, so the inherited ``last_event_sequence`` can never
    contradict the fork's event log.
    """
    return {
        "kind": "terminal",
        "last_event_sequence": boundary,
        "session_status": "interrupted",
        "output": None,
        "tool_results": [],
    }


__all__ = [
    "_ForkStorageMixin",
    "_dangling_interaction",
    "forest_from_lineage_entries",
]
