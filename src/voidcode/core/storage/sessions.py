"""Session/event storage domain.

Session rows, the append-only event stream, checkpoints, and the atomic CAS commit.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`. Public methods stay on the
facade via inheritance, so `store.<method>` addresses the same one store object.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from ...sdk.json_values import own_json_object
from ...sdk.services import SessionInfo
from ..events import EventType
from .records import (
    BACKGROUND_ACTION_SOURCE,
    MAX_CHILD_LABEL_BYTES,
    MAX_CONTINUATION_PAYLOAD_BYTES,
    MAX_WAKEUP_PAYLOAD_BYTES,
    AskAnswerConflict,
    Checkpoint,
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
    FencedSubtree,
    ObserverReceiptRecord,
    RootBudgetExhausted,
    SequenceConflict,
    SessionRecord,
    SessionStatus,
    StagedBudgetUpdate,
    StagedCancellation,
    StagedConfirmation,
    StagedContinuationDraft,
    StagedStateUpdate,
    StagedWakeup,
    StagedWakeupCancel,
    StoreConflict,
    StoredEvent,
    UnknownSession,
    WaitingAskPark,
    _canonical_json_value,
    _checkpoint_index_values,
    _ConnectionStore,
    _continuation_kind,
    _decode_checkpoint,
    _decode_event,
    _decode_session,
    _decode_session_info,
    _decode_tool_allowlist,
    _decode_waiting_ask,
    _int_value,
    _RootBudgetColumns,
    _SessionColumns,
    _stored_root_budget,
    canonical_json,
)
from .writes import apply_state_updates_locked, insert_session_event_locked, set_window_projection_locked


def _wakeup_settlement_status(events: Sequence[EventDraft]) -> Literal["completed", "failed", "cancelled"] | None:
    """The settle status a run-finished event in this commit assigns to its session's claim."""
    status: Literal["completed", "failed", "cancelled"] | None = None
    for draft in events:
        if draft.event_type == EventType.RUN_COMPLETED:
            status = "completed"
        elif draft.event_type == EventType.RUN_FAILED:
            status = "failed"
        elif draft.event_type == EventType.RUN_INTERRUPTED:
            status = "cancelled" if draft.payload.get("stop_cause") in ("user_stop", "owner_cancel") else "failed"
    return status


class _SessionsMixin(_ConnectionStore):
    """Session and event stream: session rows, event appends/readers, checkpoints, atomic commits."""

    def create_session(self, session_id: str, workspace: str, revision_id: str) -> SessionRecord:
        """Create a new session pinned to the workspace's recorded revision.

        The insert takes the revision from the workspace pointer in the SAME statement and
        requires it to still be the caller's expected revision, so a session created while a
        capability activation commits can never be pinned to the generation that just moved.
        """
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                INSERT INTO sessions (
                    session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at, last_event_sequence,
                    parent_session_id, root_session_id, depth, tool_allowlist_json
                )
                SELECT ?, ?, 'idle', w.active_revision_id, NULL, 0, ?, ?, 0, NULL, ?, 0, NULL
                FROM workspace_state w
                WHERE w.workspace = ? AND w.active_revision_id = ?
                """,
                (session_id, workspace, now, now, session_id, workspace, revision_id),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"workspace revision moved away from {revision_id!r}; retry against the current revision")
            return SessionRecord(
                session_id=session_id,
                workspace=workspace,
                status=SessionStatus.IDLE,
                active_revision_id=revision_id,
                run_id=None,
                turn=0,
                last_event_sequence=0,
                root_session_id=session_id,
            )

    def create_child_session(
        self,
        session_id: str,
        workspace: str,
        revision_id: str,
        *,
        parent_session_id: str,
        root_session_id: str,
        depth: int,
        tool_allowlist: tuple[str, ...] | None = None,
        max_root_children: int | None = None,
        birth: Mapping[str, object] | None = None,
        caller_chain: frozenset[str] | None = None,
        admission_guard: Callable[[], bool] | None = None,
    ) -> SessionRecord:
        """Create a delegated child with its immutable action identity in the birth event."""
        if not isinstance(birth, Mapping):
            raise ValueError("child reservation requires an atomic birth record")
        root_id = birth.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise ValueError("child birth record requires a captured root action identity")
        birth_label = birth.get("label")
        if birth_label is not None and (not isinstance(birth_label, str) or len(birth_label.encode("utf-8")) > MAX_CHILD_LABEL_BYTES):
            raise ValueError(f"child birth label must be UTF-8 text within {MAX_CHILD_LABEL_BYTES // 1024} KiB")
        now = int(time.time() * 1000)
        allowlist_json = None if tool_allowlist is None else json.dumps(list(tool_allowlist))
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                if max_root_children is not None or birth is not None:
                    self._connection.execute("BEGIN IMMEDIATE")
                    in_transaction = True
                guard = self._cancel_fence_guard
                if guard is not None and caller_chain is not None:
                    fenced = guard(caller_chain, None)
                    if fenced is not None:
                        raise FencedSubtree(fenced, parent_session_id)
                if admission_guard is not None and not admission_guard():
                    raise PermissionError("calling invocation is no longer active")
                if max_root_children is not None:
                    row = self._connection.execute(
                        "SELECT COUNT(*) FROM sessions WHERE root_session_id = ? AND parent_session_id IS NOT NULL",
                        (root_session_id,),
                    ).fetchone()
                    if row is not None:
                        (spawned,) = row
                        if spawned >= max_root_children:
                            raise ValueError("delegation spawn budget exhausted for this chain")
                cursor = self._connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at, last_event_sequence,
                        parent_session_id, root_session_id, depth, tool_allowlist_json
                    )
                    SELECT ?, ?, 'idle', w.active_revision_id, NULL, 0, ?, ?, 0, ?, ?, ?, ?
                    FROM workspace_state w
                    WHERE w.workspace = ? AND w.active_revision_id = ?
                    """,
                    (session_id, workspace, now, now, parent_session_id, root_session_id, depth, allowlist_json, workspace, revision_id),
                )
                if cursor.rowcount == 0:
                    raise StoreConflict(f"workspace revision moved away from {revision_id!r}; retry against the current revision")
                if birth is not None:
                    birth_payload = dict(own_json_object(birth))
                    birth_payload["eligible_owners"] = []
                    birth_payload_json = canonical_json(birth_payload)
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                        ) VALUES (?, 1, '', ?, 'core', 'plugin', ?, ?, ?)
                        """,
                        (session_id, EventType.CHILD_RESERVED, revision_id, birth_payload_json, len(birth_payload_json.encode("utf-8"))),
                    )
                    self._connection.execute(
                        "UPDATE sessions SET last_event_sequence = 1, updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                    if birth_payload.get("readonly") is True:
                        # The birth stance lands in the same transaction as the row: no window
                        # exists where a read-only child runs without its core overlay.
                        self._connection.execute(
                            """
                            INSERT INTO session_overlays (session_id, owner, tier, created_at, updated_at)
                            VALUES (?, 'core', 'read-only', ?, ?)
                            ON CONFLICT (session_id, owner) DO NOTHING
                            """,
                            (session_id, now, now),
                        )
                if in_transaction:
                    self._connection.execute("COMMIT")
                    in_transaction = False
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            return SessionRecord(
                session_id=session_id,
                workspace=workspace,
                status=SessionStatus.IDLE,
                active_revision_id=revision_id,
                run_id=None,
                turn=0,
                last_event_sequence=1 if birth is not None else 0,
                root_session_id=root_session_id,
                parent_session_id=parent_session_id,
                depth=depth,
                tool_allowlist=tool_allowlist,
            )

    def session(self, session_id: str) -> SessionRecord:
        """Fetch session metadata by session ID."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                       parent_session_id, root_session_id, depth, tool_allowlist_json
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"unknown session: {session_id}")
            return _decode_session(row)

    def descendant_sessions(self, session_id: str) -> tuple[SessionRecord, ...]:
        """Every transitive descendant of one session, in stable creation order."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            cursor = self._connection.execute(
                """
                WITH RECURSIVE subtree(session_id) AS (
                    SELECT session_id FROM sessions WHERE parent_session_id = ?
                    UNION
                    SELECT s.session_id FROM sessions s JOIN subtree t ON s.parent_session_id = t.session_id
                )
                SELECT s.session_id, s.workspace, s.status, s.active_revision_id, s.run_id, s.turn,
                       s.last_event_sequence, s.parent_session_id, s.root_session_id, s.depth, s.tool_allowlist_json
                FROM sessions s JOIN subtree t ON s.session_id = t.session_id
                ORDER BY s.rowid ASC
                """,
                (session_id,),
            )
            return tuple(_decode_session(row) for row in cursor.fetchall())

    def descendant_timestamps(self, session_id: str) -> dict[str, tuple[int, int]]:
        """Return `(created_at, updated_at)` millisecond timestamps for transitive descendants."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            cursor = self._connection.execute(
                """
                WITH RECURSIVE subtree(session_id) AS (
                    SELECT session_id FROM sessions WHERE parent_session_id = ?
                    UNION
                    SELECT s.session_id FROM sessions s JOIN subtree t ON s.parent_session_id = t.session_id
                )
                SELECT s.session_id, s.created_at, s.updated_at
                FROM sessions s JOIN subtree t ON s.session_id = t.session_id
                """,
                (session_id,),
            )
            return {str(row[0]): (int(row[1]), int(row[2])) for row in cursor.fetchall()}

    def latest_stop_payload(self, session_id: str, run_id: str | None) -> Mapping[str, object] | None:
        """Return the exact latest committed interruption payload for one run."""
        event = self.latest_stop_event(session_id, run_id)
        return None if event is None else event.payload

    def latest_stop_event(self, session_id: str, run_id: str | None) -> StoredEvent | None:
        """Return the exact latest committed interruption event for one run."""
        with self._lock:
            self._check_open()
            if not run_id:
                return None
            row = self._connection.execute(
                """
                SELECT session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json
                FROM session_events
                WHERE session_id = ? AND run_id = ? AND event_type = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (session_id, run_id, EventType.RUN_INTERRUPTED),
            ).fetchone()
            if row is None:
                return None
            return _decode_event(row)

    def latest_stop_cause(self, session_id: str, run_id: str | None) -> str | None:
        """The committed stop cause of one run, or None when no terminal fact names one."""
        payload = self.latest_stop_payload(session_id, run_id)
        cause = None if payload is None else payload.get("stop_cause")
        return cause if isinstance(cause, str) and cause else None

    def latest_tool_fact(self, session_id: str) -> tuple[str, str] | None:
        """The newest committed tool identity/outcome pair, or None when no tool finished."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT event_type, payload_json FROM session_events
                WHERE session_id = ? AND event_type IN (?, ?)
                ORDER BY sequence DESC LIMIT 1
                """,
                (session_id, EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED),
            ).fetchone()
            if row is None:
                return None
            (event_type, payload_json) = row
            try:
                payload = json.loads(payload_json)
            except ValueError, OSError:
                return None
            if not isinstance(payload, Mapping):
                return None
            call = payload.get("call")
            result = payload.get("result")
            if not isinstance(call, Mapping) or not isinstance(result, Mapping):
                return None
            name = call.get("tool_name")
            if not isinstance(name, str) or not name:
                return None
            outcome = result.get("status")
            if outcome not in ("ok", "error"):
                return None
            if event_type == EventType.TOOL_INTERRUPTED:
                outcome = "cancelled"
            return name, str(outcome)

    def session_info(self, session_id: str, owner: str) -> SessionInfo:
        """Read session, parked approval and budgets in one consistent row."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT s.session_id, s.workspace, s.status, s.active_revision_id, s.run_id,
                       s.turn, s.last_event_sequence, s.parent_session_id, s.root_session_id,
                       s.depth, s.tool_allowlist_json, a.payload_json, r.budget, r.reserved,
                       b.owner, b.since_sequence, b.max_input_tokens, b.max_output_tokens,
                       b.max_total_tokens, b.max_cost_usd, b.max_elapsed_seconds
                FROM sessions s
                LEFT JOIN checkpoints c ON c.session_id = s.session_id AND c.run_id = s.run_id
                LEFT JOIN session_events a ON a.session_id = s.session_id
                    AND a.sequence = c.approval_sequence
                    AND c.kind = 'approval_wait' AND a.event_type = ?
                LEFT JOIN root_budgets r ON r.root_id = c.root_id
                LEFT JOIN session_budgets b ON b.session_id = s.session_id AND b.owner = ?
                WHERE s.session_id = ?
                """,
                (EventType.APPROVAL_REQUESTED, owner, session_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown session: {session_id}")
            return _decode_session_info(row)

    def sessions(self, workspace: str | None = None, limit: int | None = 100) -> tuple[SessionRecord, ...]:
        """Fetch sessions, optionally filtered by workspace, ordered by updated_at descending.

        ``limit=None`` is the explicit complete-scan form used by startup publication recovery;
        ordinary callers keep the bounded default.
        """
        with self._lock:
            self._check_open()
            capped_limit = None if limit is None else max(1, limit)
            if workspace is not None:
                sql = """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    WHERE workspace = ?
                    ORDER BY updated_at DESC
                """
                params: tuple[object, ...] = (workspace,)
            else:
                sql = """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    ORDER BY updated_at DESC
                """
                params = ()
            if capped_limit is not None:
                sql += " LIMIT ?"
                params = (*params, capped_limit)
            cursor = self._connection.execute(sql, params)
            return tuple(_decode_session(row) for row in cursor.fetchall())

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

    def commit(
        self,
        session_id: str,
        *,
        expected_sequence: int,
        expected_run_id: str | None,
        run_id: str,
        revision_id: str,
        events: tuple[EventDraft, ...] | Sequence[EventDraft],
        status: SessionStatus,
        checkpoint_kind: CheckpointKind,
        checkpoint_payload: Mapping[str, object],
        start_turn: bool = False,
    ) -> SessionRecord:
        """Atomic CAS commit appending events, advancing session state, and upserting a checkpoint."""
        return self.commit_staged(
            session_id,
            expected_sequence=expected_sequence,
            expected_run_id=expected_run_id,
            run_id=run_id,
            revision_id=revision_id,
            events=events,
            status=status,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            start_turn=start_turn,
        )

    def commit_staged(
        self,
        session_id: str,
        *,
        expected_sequence: int | None = None,
        expected_run_id: str | None = None,
        run_id: str,
        revision_id: str,
        events: tuple[EventDraft, ...] | Sequence[EventDraft],
        status: SessionStatus,
        checkpoint_kind: CheckpointKind | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        staged_state: Sequence[StagedStateUpdate] = (),
        staged_continuations: Sequence[StagedContinuationDraft] = (),
        staged_cancellations: Sequence[StagedCancellation] = (),
        staged_budgets: Sequence[StagedBudgetUpdate] = (),
        staged_confirmations: Sequence[StagedConfirmation] = (),
        staged_wakeups: Sequence[StagedWakeup] = (),
        staged_wakeup_cancels: Sequence[StagedWakeupCancel] = (),
        observer_receipts: Sequence[ObserverReceiptRecord] = (),
        completed_continuation_id: str | None = None,
        completed_continuation_status: ContinuationStatus = ContinuationStatus.COMPLETED,
        checkpoint_bindings: Sequence[tuple[str, str]] = (),
        root_action_id: str | None = None,
        root_action_budget: int | None = None,
        root_budget_grant: tuple[str, int, str] | None = None,
        root_reservation: tuple[str, int | None] | None = None,
        start_turn: bool = False,
        ask_answer_guard: str | None = None,
    ) -> SessionRecord:
        """Atomic CAS commit applying staged state updates, continuations, event drafts, and checkpoint.

        Each `(payload_field, event_type)` binding in `checkpoint_bindings` is
        assigned the actual appended sequence of the first event of that type in
        THIS transaction, so a checkpoint reference can never point at a guessed
        or not-yet-committed sequence.
        """

        if staged_wakeups:
            if not isinstance(root_action_id, str) or not root_action_id:
                raise StoreConflict("staged wakeups require a captured root action identity")
            if root_action_budget is not None and (type(root_action_budget) is not int or root_action_budget < 0):
                raise StoreConflict("staged wakeups have an invalid captured root budget")
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                if staged_wakeups:
                    assert root_action_id is not None
                    wakeup_budget_row = self._connection.execute(
                        "SELECT budget FROM root_budgets WHERE root_id = ?",
                        (root_action_id,),
                    ).fetchone()
                    wakeup_budget = _stored_root_budget(wakeup_budget_row)
                    if root_action_budget is None and wakeup_budget is not None:
                        raise StoreConflict("staged wakeup captured an uncapped root with a finite budget row")
                    if root_action_budget is not None and (wakeup_budget is None or wakeup_budget < root_action_budget):
                        raise StoreConflict("staged wakeup lost its captured finite root authorization")

                if root_budget_grant is not None:
                    root_id, amount, grant_id = root_budget_grant
                    prior = self._connection.execute(
                        "SELECT root_id, amount FROM root_grants WHERE grant_id = ?",
                        (grant_id,),
                    ).fetchone()
                    if prior is not None:
                        (prior_root_id, prior_amount) = prior
                        if prior_root_id != root_id or prior_amount != amount:
                            raise StoreConflict("root action grant id was already used for a different authorization")
                    else:
                        state = self._connection.execute(
                            "SELECT budget FROM root_budgets WHERE root_id = ?",
                            (root_id,),
                        ).fetchone()
                        if state is None:
                            raise StoreConflict("there is no finite root action budget to extend")
                        (existing_root_budget,) = state
                        if existing_root_budget + amount > 9_223_372_036_854_775_807:
                            raise ValueError("root action budget extension exceeds SQLite INTEGER range")
                        self._connection.execute(
                            "UPDATE root_budgets SET budget = budget + ?, updated_at = ? WHERE root_id = ?",
                            (amount, int(time.time() * 1000), root_id),
                        )
                        self._connection.execute(
                            "INSERT INTO root_grants (grant_id, root_id, amount, created_at) VALUES (?, ?, ?, ?)",
                            (grant_id, root_id, amount, int(time.time() * 1000)),
                        )

                if root_reservation is not None:
                    root_id, root_limit = root_reservation
                    reserved_at = int(time.time() * 1000)
                    if root_limit is not None:
                        self._connection.execute(
                            """
                            INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                            VALUES (?, ?, 0, ?, ?)
                            """,
                            (root_id, root_limit, reserved_at, reserved_at),
                        )
                    state = self._connection.execute(
                        "SELECT budget, reserved FROM root_budgets WHERE root_id = ?",
                        (root_id,),
                    ).fetchone()
                    if state is not None:
                        reserved = self._connection.execute(
                            """
                            UPDATE root_budgets
                            SET reserved = reserved + 1, updated_at = ?
                            WHERE root_id = ? AND reserved < budget
                            """,
                            (reserved_at, root_id),
                        )
                        if reserved.rowcount == 0:
                            counters = _RootBudgetColumns(*state)
                            raise RootBudgetExhausted(root_id, counters.budget, counters.reserved)

                cursor = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions
                    WHERE session_id = ?
                    """,
                    (session_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise UnknownSession(f"session {session_id!r} does not exist", session_id)

                session_row = _SessionColumns(*row)

                if expected_sequence is not None and session_row.last_event_sequence != expected_sequence:
                    raise SequenceConflict(
                        f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {session_row.last_event_sequence}"
                    )
                if expected_run_id is not None and session_row.run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {session_row.run_id!r}")
                if session_row.active_revision_id != revision_id:
                    raise StoreConflict(
                        f"active revision mismatch for session {session_id!r}: active is {session_row.active_revision_id!r}, got {revision_id!r}"
                    )
                if ask_answer_guard is not None:
                    decided = self._connection.execute(
                        "SELECT 1 FROM ask_answers WHERE session_id = ? AND request_id = ? LIMIT 1",
                        (session_id, ask_answer_guard),
                    ).fetchone()
                    if decided is not None:
                        raise AskAnswerConflict(session_id, ask_answer_guard)
                now = int(time.time() * 1000)

                # Apply staged state updates
                apply_state_updates_locked(self._connection, staged_state, default_revision_id=revision_id, now=now)

                # Apply staged continuation operations in their recorded order, so
                # cancel-then-enqueue keeps the new continuation while enqueue-then-cancel
                # (including by returned id) cancels exactly the one that was enqueued.
                ops: list[tuple[int, Literal["enqueue", "cancel"], StagedContinuationDraft | StagedCancellation]] = []
                ops.extend((c.ordinal, "enqueue", c) for c in staged_continuations)
                ops.extend((c.ordinal, "cancel", c) for c in staged_cancellations)
                for _ord, _kind, payload in sorted(ops, key=lambda item: item[0]):
                    if isinstance(payload, StagedContinuationDraft):
                        cont = payload
                        cont_payload_json = canonical_json(cont.payload)
                        if cont.idempotency_key is not None:
                            # Model rows and background rows live in separate tables: one
                            # owner/session/key names each independently, and only rows
                            # of the same table dedupe.
                            if cont.source == BACKGROUND_ACTION_SOURCE:
                                c_chk = self._connection.execute(
                                    """
                                    SELECT action_id FROM background_actions
                                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                                    """,
                                    (session_id, cont.owner, cont.idempotency_key),
                                )
                            else:
                                c_chk = self._connection.execute(
                                    """
                                    SELECT continuation_id FROM continuations
                                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                                    """,
                                    (session_id, cont.owner, cont.idempotency_key),
                                )
                            if c_chk.fetchone() is not None:
                                continue
                        continuation_size = len(cont_payload_json.encode("utf-8"))
                        if continuation_size > MAX_CONTINUATION_PAYLOAD_BYTES:
                            raise StoreConflict(
                                f"continuation {cont.continuation_id!r} payload is {continuation_size} bytes, "
                                f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
                            )
                        if cont.source == BACKGROUND_ACTION_SOURCE:
                            draft = cont.payload
                            if "root_action_budget" not in draft:
                                raise StoreConflict(f"background action {cont.continuation_id!r} has no captured root budget")
                            staged_budget = draft["root_action_budget"]
                            if staged_budget is not None and (type(staged_budget) is not int or not 0 <= staged_budget <= 9_223_372_036_854_775_807):
                                raise StoreConflict(f"background action {cont.continuation_id!r} has an invalid captured root budget")
                            if not isinstance(cont.root_id, str) or not cont.root_id:
                                raise StoreConflict(f"background action {cont.continuation_id!r} has no captured root authorization")
                            staged_authority = draft.get("tool_wait_authority")
                            try:
                                self._connection.execute(
                                    """
                                    INSERT INTO background_actions (
                                        action_id, owner, session_id, revision_id, idempotency_key, root_id,
                                        root_action_budget, status, payload_json, authority_json, claim_json,
                                        error, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, ?, ?)
                                    """,
                                    (
                                        cont.continuation_id,
                                        cont.owner,
                                        session_id,
                                        revision_id,
                                        cont.idempotency_key,
                                        cont.root_id,
                                        staged_budget,
                                        _canonical_json_value(draft.get("action")),
                                        None if staged_authority is None else _canonical_json_value(staged_authority),
                                        now,
                                        now,
                                    ),
                                )
                            except sqlite3.IntegrityError as exc:
                                raise StoreConflict(f"background action {cont.continuation_id!r} already exists") from exc
                        else:
                            try:
                                self._connection.execute(
                                    """
                                    INSERT INTO continuations (
                                        continuation_id, owner, session_id, source, kind, run_id, revision_id,
                                        idempotency_key, root_id, role, status, payload_json, created_at, updated_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                                    """,
                                    (
                                        cont.continuation_id,
                                        cont.owner,
                                        session_id,
                                        cont.source,
                                        _continuation_kind(cont.payload),
                                        run_id,
                                        revision_id,
                                        cont.idempotency_key,
                                        cont.root_id,
                                        cont.role if cont.role in ("system", "user") else "system",
                                        cont_payload_json,
                                        now,
                                        now,
                                    ),
                                )
                            except sqlite3.IntegrityError as exc:
                                raise StoreConflict(f"continuation {cont.continuation_id!r} already exists") from exc
                    elif isinstance(payload, StagedCancellation):
                        canc = payload
                        if canc.continuation_id is not None:
                            self._connection.execute(
                                """
                                UPDATE continuations
                                SET status = 'cancelled', updated_at = ?
                                WHERE session_id = ? AND owner = ? AND continuation_id = ?
                                  AND status IN ('pending', 'claimed') AND (
                                      ? = 0 OR NOT EXISTS (
                                          SELECT 1 FROM continuation_starts
                                          WHERE continuation_starts.session_id = continuations.session_id
                                            AND continuation_starts.continuation_id = continuations.continuation_id
                                      )
                                  )
                                """,
                                (
                                    now,
                                    session_id,
                                    canc.owner,
                                    canc.continuation_id,
                                    int(canc.only_if_unstarted),
                                ),
                            )
                        else:
                            # Background actions are cancelled through their own abort-aware path;
                            # a blanket model-continuation cancel must not strand live drivers.
                            self._connection.execute(
                                """
                                UPDATE continuations
                                SET status = 'cancelled', updated_at = ?
                                WHERE session_id = ? AND owner = ? AND status IN ('pending', 'claimed')
                                """,
                                (now, session_id, canc.owner),
                            )

                # Apply staged budgets
                for bu in staged_budgets:
                    if bu.action == "clear":
                        self._connection.execute(
                            "DELETE FROM session_budgets WHERE session_id = ? AND owner = ?",
                            (session_id, bu.owner),
                        )
                    elif bu.action == "set" and bu.budget is not None:
                        self._connection.execute(
                            """
                            INSERT INTO session_budgets (
                                session_id, owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens,
                                max_cost_usd, max_elapsed_seconds, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (session_id, owner) DO UPDATE SET
                                since_sequence = excluded.since_sequence,
                                max_input_tokens = excluded.max_input_tokens,
                                max_output_tokens = excluded.max_output_tokens,
                                max_total_tokens = excluded.max_total_tokens,
                                max_cost_usd = excluded.max_cost_usd,
                                max_elapsed_seconds = excluded.max_elapsed_seconds,
                                updated_at = excluded.updated_at
                            """,
                            (
                                session_id,
                                bu.owner,
                                bu.budget.since_sequence,
                                bu.budget.max_input_tokens,
                                bu.budget.max_output_tokens,
                                bu.budget.max_total_tokens,
                                bu.budget.max_cost_usd,
                                bu.budget.max_elapsed_seconds,
                                now,
                                now,
                            ),
                        )

                # Create pending action confirmations with the event that announces them.
                for confirmation in staged_confirmations:
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO action_confirmations (
                                request_id, session_id, owner, command_name, arguments_json, revision_id,
                                expected_state_version, ui_type, ui_data_json, status, created_at, decided_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                            """,
                            (
                                confirmation.request_id,
                                session_id,
                                confirmation.owner,
                                confirmation.command_name,
                                canonical_json(confirmation.arguments),
                                revision_id,
                                confirmation.expected_state_version,
                                confirmation.ui_type,
                                canonical_json(confirmation.ui_data),
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise StoreConflict(f"confirmation {confirmation.request_id!r} already exists") from exc

                # Schedule or cancel owner wakeups with the callback that asked for them.
                if staged_wakeups and (not isinstance(root_action_id, str) or not root_action_id):
                    raise StoreConflict("staged wakeups require the run's captured root action identity")
                for wakeup in staged_wakeups:
                    wakeup_size = len(canonical_json(wakeup.payload).encode("utf-8"))
                    if wakeup_size > MAX_WAKEUP_PAYLOAD_BYTES:
                        raise StoreConflict(
                            f"wakeup {wakeup.wakeup_id!r} payload is {wakeup_size} bytes, "
                            f"exceeding the {MAX_WAKEUP_PAYLOAD_BYTES}-byte wakeup-payload bound"
                        )
                    try:
                        self._connection.execute(
                            """
                            INSERT INTO wakeups (
                                wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget,
                                role, due_at, status, claim_run_id, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                            ON CONFLICT (wakeup_id) DO NOTHING
                            """,
                            (
                                wakeup.wakeup_id,
                                wakeup.owner,
                                session_row.workspace,
                                wakeup.session_id,
                                canonical_json(wakeup.payload),
                                root_action_id,
                                root_action_budget,
                                wakeup.role if wakeup.role in ("system", "user") else "system",
                                wakeup.due_at,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise StoreConflict(f"wakeup {wakeup.wakeup_id!r} already exists") from exc
                for cancel in staged_wakeup_cancels:
                    self._connection.execute(
                        """
                        UPDATE wakeups SET status = 'cancelled', updated_at = ?
                        WHERE wakeup_id = ? AND owner = ? AND status = 'pending'
                        """,
                        (now, cancel.wakeup_id, cancel.owner),
                    )

                # Apply observer receipts
                for r in observer_receipts:
                    if r.require_interruption_source:
                        if not isinstance(r.expected_run_id, str) or not r.expected_run_id:
                            raise StoreConflict("an interruption receipt has no exact child run identity")
                        source = self._connection.execute(
                            """
                            SELECT event_type, run_id FROM session_events
                            WHERE session_id = ? AND sequence = ?
                            """,
                            (r.session_id, r.event_sequence),
                        ).fetchone()
                        if source is None or source[0] != EventType.RUN_INTERRUPTED or source[1] != r.expected_run_id:
                            raise StoreConflict("child interruption receipt does not match its exact source event")
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO observer_receipts (session_id, event_sequence, owner, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (r.session_id, r.event_sequence, r.owner, now),
                    )

                # Update completed continuation
                if completed_continuation_id is not None:
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = ?, updated_at = ?
                        WHERE continuation_id = ?
                        """,
                        (completed_continuation_status.value, now, completed_continuation_id),
                    )

                # Append events
                current_seq = session_row.last_event_sequence
                first_sequence_by_type: dict[str, int] = {}
                for draft in events:
                    if draft.event_type == EventType.WINDOW_PROJECTED:
                        projection = draft.payload
                        set_window_projection_locked(
                            self._connection,
                            session_id,
                            str(projection["owner"]),
                            covered_from_sequence=_int_value(projection["covered_from_sequence"], "covered_from_sequence"),
                            covered_to_sequence=_int_value(projection["covered_to_sequence"], "covered_to_sequence"),
                            replacement=str(projection["replacement"]),
                            covered_bytes=_int_value(projection["covered_bytes"], "covered_bytes"),
                            dropped_bytes=_int_value(projection["dropped_bytes"], "dropped_bytes"),
                        )
                    current_seq += 1
                    first_sequence_by_type.setdefault(draft.event_type, current_seq)
                    insert_session_event_locked(
                        self._connection,
                        session_id,
                        current_seq,
                        run_id=run_id,
                        revision_id=revision_id,
                        draft=draft,
                        now=now,
                    )

                new_turn = session_row.turn + 1 if start_turn else session_row.turn

                # Update session
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?,
                        run_id = ?,
                        turn = ?,
                        updated_at = ?,
                        last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (status.value, run_id, new_turn, now, current_seq, session_id),
                )

                if checkpoint_kind is not None and checkpoint_payload is not None:
                    bound_payload = dict(checkpoint_payload)
                    for bound_field, bound_event_type in checkpoint_bindings:
                        bound_sequence = first_sequence_by_type.get(bound_event_type)
                        if bound_sequence is None:
                            raise StoreConflict(f"checkpoint binding {bound_field!r} has no {bound_event_type!r} event in this commit")
                        bound_payload[bound_field] = bound_sequence
                    checkpoint_json = canonical_json(bound_payload)
                    checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(bound_payload)
                    self._connection.execute(
                        """
                        INSERT INTO checkpoints (
                            session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                            root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (session_id) DO UPDATE SET
                            kind = excluded.kind,
                            revision_id = excluded.revision_id,
                            run_id = excluded.run_id,
                            last_event_sequence = excluded.last_event_sequence,
                            payload_json = excluded.payload_json,
                            root_id = excluded.root_id,
                            approval_sequence = excluded.approval_sequence,
                            ask_sequence = excluded.ask_sequence,
                            ask_request_id = excluded.ask_request_id,
                            updated_at = excluded.updated_at
                        """,
                        (
                            session_id,
                            checkpoint_kind.value,
                            revision_id,
                            run_id,
                            current_seq,
                            checkpoint_json,
                            checkpoint_root_id,
                            checkpoint_approval,
                            checkpoint_ask_seq,
                            checkpoint_ask_req,
                            now,
                        ),
                    )
                # A run-finished event ends the run that owns this session's claimed wakeup.
                # Settlement is atomic with that event, so a parked approval/ask (no run-finished
                # event) keeps its claim across resume while a terminal/resumable finish never
                # leaves the row claimed forever.
                wakeup_status = _wakeup_settlement_status(events)
                if wakeup_status is not None:
                    self._connection.execute(
                        "UPDATE wakeups SET status = ?, updated_at = ? WHERE session_id = ? AND status = 'claimed'",
                        (wakeup_status, now, session_id),
                    )
                self._connection.execute("COMMIT")
                in_transaction = False

                return SessionRecord(
                    session_id=session_id,
                    workspace=session_row.workspace,
                    status=status,
                    active_revision_id=revision_id,
                    run_id=run_id,
                    turn=new_turn,
                    root_session_id=session_row.root_session_id,
                    parent_session_id=session_row.parent_session_id,
                    depth=session_row.depth,
                    tool_allowlist=_decode_tool_allowlist(session_row.tool_allowlist_json),
                    last_event_sequence=current_seq,
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
