"""Model continuation storage domain.

Queued model messages: enqueue, claim/start, child-message steering, terminal settlement,
observer receipts, and root-action budgets. User follow-ups live in `followups`; wakeups
live in `wakeups`; driver-run background actions live in `background_actions`.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`. Public methods stay on the
facade via inheritance, so `store.<method>` addresses the same one store object.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from ...sdk.json_values import own_json_object
from ..events import CHILD_MESSAGE_OWNER, EventType
from ._database import _ConnectionStore
from ._rows import (
    _checkpoint_index_values,
    _CheckpointGuardColumns,
    _ChildClaimGuardColumns,
    _continuation_kind,
    _ContinuationColumns,
    _decode_closed_enum,
    _decode_continuation,
    _decode_tool_allowlist,
    _RootBudgetColumns,
    _SessionColumns,
)
from ._values import (
    BACKGROUND_ACTION_SOURCE,
    MAX_CONTINUATION_PAYLOAD_BYTES,
    CheckpointKind,
    ContinuationRecord,
    ContinuationStatus,
    EventDraft,
    FencedSubtree,
    SessionRecord,
    SessionStatus,
    StoreConflict,
    StoredEvent,
    UnknownContinuation,
    UnknownSession,
    canonical_json,
)
from .writes import insert_session_event_locked, read_session, record_event_indexes


class _ContinuationsMixin(_ConnectionStore):
    """Queued model work: continuations, child-message claims, and root budgets."""

    def has_queued_child_followup(self, session_id: str) -> bool:
        """Whether one session holds a pending core child follow-up (never notices or claims)."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = ?
                  AND kind = 'child_followup'
                LIMIT 1
                """,
                (session_id, CHILD_MESSAGE_OWNER),
            ).fetchone()
            return row is not None

    def child_message_ids(self, session_id: str) -> tuple[str, ...]:
        """Capture exact pending or claimed core child-message identities."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT continuation_id FROM continuations
                WHERE session_id = ? AND status IN ('pending', 'claimed') AND owner = ?
                ORDER BY rowid ASC
                """,
                (session_id, CHILD_MESSAGE_OWNER),
            ).fetchall()
            return tuple(continuation_id for (continuation_id,) in rows)

    def child_wait_registrations(self, child_session_ids: Sequence[str]) -> tuple[tuple[SessionRecord, tuple[str, ...]], ...]:
        """Snapshot current child runs and their registered child messages atomically."""
        with self._lock:
            self._check_open()
            self._connection.execute("BEGIN")
            try:
                snapshots = tuple(
                    (read_session(self._connection, child_session_id), self.child_message_ids(child_session_id))
                    for child_session_id in child_session_ids
                )
                self._connection.execute("COMMIT")
                return snapshots
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def enqueue_continuation(
        self,
        continuation_id: str,
        owner: str,
        session_id: str,
        source: str,
        run_id: str,
        revision_id: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        root_id: str | None = None,
        root_budget: int | None = None,
        role: str = "system",
        caller_chain: frozenset[str] | None = None,
        admission_guard: Callable[[], bool] | None = None,
    ) -> str:
        """Queue a continuation with its stable action-chain identity."""
        if source == BACKGROUND_ACTION_SOURCE:
            raise ValueError("enqueue_continuation is model-only; queue background actions with enqueue_background_action")
        if role not in ("system", "user"):
            raise ValueError("continuation role must be 'system' or 'user'")
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict("continuation requires a captured root action identity")
        payload_json = canonical_json(payload)
        with self._lock:
            self._check_open()
            guard = self._cancel_fence_guard
            if guard is not None and owner == CHILD_MESSAGE_OWNER and payload.get("kind") == "child_followup":
                chain = caller_chain
                if chain is None:
                    ancestors = self._connection.execute(
                        """
                        WITH RECURSIVE ancestors(session_id, parent_session_id) AS (
                            SELECT session_id, parent_session_id FROM sessions WHERE session_id = ?
                            UNION
                            SELECT s.session_id, s.parent_session_id FROM sessions s
                            JOIN ancestors a ON s.session_id = a.parent_session_id
                        )
                        SELECT session_id FROM ancestors
                        """,
                        (session_id,),
                    ).fetchall()
                    chain = frozenset(session_ancestor for (session_ancestor,) in ancestors)
                fenced = guard(chain, session_id)
                if fenced is not None:
                    raise FencedSubtree(fenced, session_id)
            if admission_guard is not None and not admission_guard():
                raise PermissionError("calling invocation is no longer active")
            now = int(time.time() * 1000)
            if idempotency_key is not None:
                cursor = self._connection.execute(
                    """
                    SELECT continuation_id, root_id, payload_json FROM continuations
                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                    """,
                    (session_id, owner, idempotency_key),
                )
                row = cursor.fetchone()
                if row is not None:
                    existing_id, existing_root_id, existing_payload_json = row
                    if existing_root_id != root_id or existing_payload_json != payload_json:
                        raise StoreConflict("continuation idempotency key was already used with a different authorization or payload")
                    return existing_id
            continuation_size = len(payload_json.encode("utf-8"))
            if continuation_size > MAX_CONTINUATION_PAYLOAD_BYTES:
                raise StoreConflict(
                    f"continuation {continuation_id!r} payload is {continuation_size} bytes, "
                    f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
                )
            if root_budget is not None:
                if type(root_budget) is not int or not 0 <= root_budget <= 9_223_372_036_854_775_807:
                    raise ValueError("root_budget must fit a non-negative SQLite INTEGER")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                    VALUES (?, ?, 0, ?, ?)
                    """,
                    (root_id, root_budget, now, now),
                )
            try:
                self._connection.execute(
                    """
                    INSERT INTO continuations (
                        continuation_id, owner, session_id, source, kind, run_id, revision_id,
                        idempotency_key, root_id, role, status, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        continuation_id,
                        owner,
                        session_id,
                        source,
                        _continuation_kind(payload),
                        run_id,
                        revision_id,
                        idempotency_key,
                        root_id,
                        role,
                        payload_json,
                        now,
                        now,
                    ),
                )
                return continuation_id
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(f"continuation {continuation_id!r} already exists") from exc

    def claim_child_messages(
        self,
        session_id: str,
        run_id: str,
        *,
        revision_id: str,
        limit: int = 8,
    ) -> tuple[StoredEvent, ...]:
        """Claim pending core child messages into the CURRENT running run.

        This is the steering boundary: the caller has just committed the whole native batch and
        is about to assemble the next provider request, so the message is appended to the same
        run and rendered by the next context assembly. Eligibility is checked in the same
        transaction as the claim: a session that is waiting, interrupted, terminal, on another
        revision, or driven by another run cannot consume the message here.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                row = self._connection.execute(
                    """
                    SELECT status, active_revision_id, run_id, last_event_sequence
                    FROM sessions WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise UnknownSession(f"session {session_id!r} does not exist", session_id)
                guard_row = _ChildClaimGuardColumns(*row)
                sequence = guard_row.last_event_sequence
                guard_status = _decode_closed_enum(SessionStatus, guard_row.status, field="status", record=f"session {session_id!r}")
                if guard_status is not SessionStatus.RUNNING or guard_row.run_id != run_id or guard_row.active_revision_id != revision_id:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return ()
                rows = self._connection.execute(
                    """
                    SELECT continuation_id, source, payload_json, role
                    FROM continuations
                    WHERE session_id = ? AND status = 'pending' AND owner = ?
                      AND kind IN ('child_followup', 'child_finished')
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT ?
                    """,
                    (session_id, CHILD_MESSAGE_OWNER, max(1, limit)),
                ).fetchall()
                if not rows:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return ()
                now = int(time.time() * 1000)
                claimed: list[StoredEvent] = []
                for continuation_id, source, payload_json, role in rows:
                    # The appended event IS the delivery: the message now belongs to this run's
                    # committed prefix, so completing the row prevents any later drain from
                    # starting it again. A crash before the next dispatch resumes from the
                    # checkpoint with the event already in context.
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'completed', run_id = ?, updated_at = ?
                        WHERE continuation_id = ? AND status = 'pending'
                        """,
                        (run_id, now, continuation_id),
                    )
                    sequence += 1
                    payload = own_json_object(json.loads(payload_json))
                    child_payload_json = canonical_json({"continuation_id": continuation_id, "payload": payload, "role": str(role)})
                    self._connection.execute(
                        """
                    INSERT INTO session_events (
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?, ?)
                    """,
                        (
                            session_id,
                            sequence,
                            run_id,
                            EventType.CONTINUATION_STARTED,
                            source,
                            revision_id,
                            child_payload_json,
                            len(child_payload_json.encode("utf-8")),
                        ),
                    )
                    record_event_indexes(self._connection, session_id, EventType.CONTINUATION_STARTED, {"continuation_id": continuation_id}, now)
                    claimed.append(
                        StoredEvent(
                            session_id=session_id,
                            sequence=sequence,
                            run_id=run_id,
                            event_type=EventType.CONTINUATION_STARTED,
                            source=source,
                            actor="plugin",
                            revision_id=revision_id,
                            payload={"continuation_id": continuation_id, "payload": payload, "role": str(role)},
                        )
                    )
                self._connection.execute(
                    "UPDATE sessions SET last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                    (sequence, now, session_id),
                )
                # The appended messages are part of the resumable prefix: a crash before the next
                # provider request must render them, not reject them as intervening events.
                self._connection.execute(
                    "UPDATE checkpoints SET last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                    (sequence, now, session_id),
                )
                self._connection.execute("COMMIT")
                in_transaction = False
                return tuple(claimed)
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def ensure_root_budget(self, root_id: str, budget: int | None) -> None:
        """Record a finite root action authorization once; `None` means no action cap."""
        if budget is None:
            return
        if type(budget) is not int or not 0 <= budget <= 9_223_372_036_854_775_807:
            raise ValueError("root_action_budget must fit a non-negative SQLite INTEGER")
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO root_budgets (root_id, budget, reserved, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (root_id, budget, now, now),
            )

    def root_budget_state(self, root_id: str) -> tuple[int, int] | None:
        """Return (budget, reserved) for a root action chain, or None when unknown."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT budget, reserved FROM root_budgets WHERE root_id = ?",
                (root_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            counters = _RootBudgetColumns(*row)
            return (counters.budget, counters.reserved)

    def continuation_id_for_idempotency(self, session_id: str, owner: str, idempotency_key: str) -> str | None:
        """Return the durable model-continuation id already recorded for an idempotency key.

        Background actions live in their own table with their own namespace: the same
        owner/session/key may name one model message AND one background action, and
        neither lookup may swallow the other (ordinary keys are arbitrary strings).
        """
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id FROM continuations
                WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                """,
                (session_id, owner, idempotency_key),
            )
            row = cursor.fetchone()
            return None if row is None else row[0]

    def continuation_status(self, continuation_id: str) -> ContinuationStatus | None:
        """Return the durable status of one continuation row, or None when absent."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT status FROM continuations WHERE continuation_id = ?",
                (continuation_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _decode_closed_enum(ContinuationStatus, row[0], field="status", record=f"continuation {continuation_id!r}")

    def continuation_owner(self, continuation_id: str) -> str | None:
        """Return the owning plugin of one continuation row, or None when absent."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT owner FROM continuations WHERE continuation_id = ?",
                (continuation_id,),
            )
            row = cursor.fetchone()
            return None if row is None else row[0]

    def pending_continuations(
        self,
        session_id: str,
        limit: int = 1,
    ) -> tuple[ContinuationRecord, ...]:
        """Fetch pending model continuations in order of creation (never background actions)."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                FROM continuations
                WHERE session_id = ? AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            )
            return tuple(_decode_continuation(row) for row in cursor.fetchall())

    def claimed_continuations(
        self,
        session_id: str,
        limit: int = 100,
    ) -> tuple[ContinuationRecord, ...]:
        """Fetch previously claimed model continuations for recovery (never background actions)."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                       idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                FROM continuations
                WHERE session_id = ? AND status = 'claimed'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, max(1, limit)),
            )
            return tuple(_decode_continuation(row) for row in cursor.fetchall())

    def complete_continuation(self, continuation_id: str, status: ContinuationStatus = ContinuationStatus.COMPLETED) -> None:
        """Mark a claimed continuation as completed, failed, or cancelled."""
        if status not in (ContinuationStatus.COMPLETED, ContinuationStatus.FAILED, ContinuationStatus.CANCELLED):
            raise ValueError(f"continuation completion must be terminal, got {status.value!r}")
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE continuations
                SET status = ?, updated_at = ?
                WHERE continuation_id = ?
                """,
                (status.value, now, continuation_id),
            )
            if cursor.rowcount == 0:
                raise UnknownContinuation(f"continuation {continuation_id!r} does not exist", continuation_id)

    def claim_and_start_continuation(
        self,
        session_id: str,
        run_id: str,
        logical_turn: str,
        *,
        revision_id: str,
        caller_chain: frozenset[str],
        initial_root_budget: int | None = None,
        max_steps: int | None = None,
        continuation_id: str | None = None,
        automatic: bool = False,
    ) -> tuple[ContinuationRecord, SessionRecord]:
        """Atomically claim ONE continuation, emit continuation_started, advance session, and commit ready CP.

        With `continuation_id`, only that exact row is eligible: a caller that reserved an
        identity before enqueuing never starts somebody else's older message.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True

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
                session_status = _decode_closed_enum(SessionStatus, session_row.status, field="status", record=f"session {session_id!r}")
                if session_row.active_revision_id != revision_id:
                    raise StoreConflict(
                        f"active revision mismatch for session {session_id!r}: active is {session_row.active_revision_id!r}, got {revision_id!r}"
                    )

                checkpoint_query_row = self._connection.execute(
                    "SELECT kind, run_id, payload_json FROM checkpoints WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                checkpoint_guard = None if checkpoint_query_row is None else _CheckpointGuardColumns(*checkpoint_query_row)
                guard_kind = (
                    None
                    if checkpoint_guard is None
                    else _decode_closed_enum(CheckpointKind, checkpoint_guard.kind, field="kind", record=f"checkpoint of session {session_id!r}")
                )
                now = int(time.time() * 1000)

                c_cursor = self._connection.execute(
                    """
                    SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                           idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                    FROM continuations
                    WHERE session_id = ? AND status = 'pending'
                      AND owner <> 'core'
                      AND (? IS NULL OR continuation_id = ?)
                      AND NOT (? AND owner = ? AND COALESCE(kind, '') = 'child_finished')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (session_id, continuation_id, continuation_id, automatic and session_status is SessionStatus.INTERRUPTED, CHILD_MESSAGE_OWNER),
                )
                pending_row = c_cursor.fetchone()
                cont_row = None if pending_row is None else _ContinuationColumns(*pending_row)
                guard = self._cancel_fence_guard
                if guard is not None and cont_row is not None and cont_row.owner == CHILD_MESSAGE_OWNER:
                    fenced = guard(caller_chain, session_id)
                    if fenced is not None:
                        raise FencedSubtree(fenced, session_id)
                if cont_row is None:
                    c_cursor2 = self._connection.execute(
                        """
                        SELECT continuation_id, owner, session_id, source, run_id, revision_id,
                               idempotency_key, root_id, status, payload_json, created_at, updated_at, role
                        FROM continuations
                        WHERE session_id = ? AND status = 'claimed'
                          AND owner <> 'core'
                          AND (? IS NULL OR continuation_id = ?)
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (session_id, continuation_id, continuation_id),
                    )
                    claimed_row = c_cursor2.fetchone()
                    if claimed_row is None:
                        raise StoreConflict(f"no pending or claimed continuations for session {session_id!r}")
                    cont_row = _ContinuationColumns(*claimed_row)
                    if checkpoint_guard is None:
                        raise StoreConflict(f"claimed continuation {cont_row.continuation_id!r} has no resumable checkpoint")
                    if guard_kind is CheckpointKind.TERMINAL:
                        raise StoreConflict(f"claimed continuation {cont_row.continuation_id!r} has a terminal checkpoint")
                    try:
                        cp_payload = json.loads(checkpoint_guard.payload_json)
                    except (TypeError, ValueError) as exc:
                        raise StoreConflict(f"claimed continuation {cont_row.continuation_id!r} has a malformed checkpoint") from exc
                    if not isinstance(cp_payload, Mapping):
                        raise StoreConflict(f"claimed continuation {cont_row.continuation_id!r} has a malformed checkpoint")
                    if (
                        checkpoint_guard.run_id != cont_row.run_id
                        or cp_payload.get("continuation_id") != cont_row.continuation_id
                        or not cont_row.root_id
                        or cp_payload.get("root_id") != cont_row.root_id
                    ):
                        raise StoreConflict(f"claimed continuation {cont_row.continuation_id!r} does not own its checkpoint/root action identity")
                    claimed_record = _decode_continuation(claimed_row)
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return claimed_record, SessionRecord(
                        session_id=session_id,
                        workspace=session_row.workspace,
                        status=session_status,
                        active_revision_id=session_row.active_revision_id,
                        run_id=session_row.run_id,
                        turn=session_row.turn,
                        last_event_sequence=session_row.last_event_sequence,
                        root_session_id=session_row.root_session_id,
                        parent_session_id=session_row.parent_session_id,
                        depth=session_row.depth,
                        tool_allowlist=_decode_tool_allowlist(session_row.tool_allowlist_json),
                    )

                if not cont_row.root_id:
                    raise StoreConflict(f"continuation {cont_row.continuation_id!r} has no captured root action identity")
                if session_status not in (
                    SessionStatus.IDLE,
                    SessionStatus.COMPLETED,
                    SessionStatus.FAILED,
                    SessionStatus.INTERRUPTED,
                ):
                    raise StoreConflict(f"session {session_id!r} already holds active work ({session_status.value})")
                if session_status is SessionStatus.IDLE and session_row.parent_session_id is not None:
                    # A spawned child is idle only before its first run commits; its first
                    # boundary merges the queued follow-up instead of racing a second start.
                    raise StoreConflict(f"child session {session_id!r} has not committed its spawn run")
                if guard_kind is not None and guard_kind is not CheckpointKind.TERMINAL:
                    raise StoreConflict(f"session {session_id!r} holds a resumable/parked checkpoint ({guard_kind.value})")
                claimed_other = self._connection.execute(
                    "SELECT 1 FROM continuations WHERE session_id = ? AND status = 'claimed' LIMIT 1",
                    (session_id,),
                ).fetchone()
                if claimed_other is not None:
                    raise StoreConflict(f"session {session_id!r} already has claimed work")
                cid = cont_row.continuation_id
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET status = 'claimed', run_id = ?, updated_at = ?
                    WHERE continuation_id = ?
                    """,
                    (run_id, now, cid),
                )

                assert pending_row is not None
                cont_record = replace(
                    _decode_continuation(pending_row),
                    run_id=run_id,
                    revision_id=revision_id,
                    status=ContinuationStatus.CLAIMED,
                    updated_at=now,
                )

                new_seq = session_row.last_event_sequence + 1
                disp_payload = canonical_json(
                    {"continuation_id": cont_record.continuation_id, "payload": cont_record.payload, "role": cont_record.role}
                )
                self._connection.execute(
                    """
                    INSERT INTO session_events (
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?, ?)
                    """,
                    (
                        session_id,
                        new_seq,
                        run_id,
                        EventType.CONTINUATION_STARTED,
                        f"plugin.{cont_record.owner}",
                        revision_id,
                        disp_payload,
                        len(disp_payload.encode("utf-8")),
                    ),
                )
                record_event_indexes(
                    self._connection, session_id, EventType.CONTINUATION_STARTED, {"continuation_id": cont_record.continuation_id}, now
                )

                new_turn = session_row.turn + 1
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'running',
                        run_id = ?,
                        turn = ?,
                        updated_at = ?,
                        last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (run_id, new_turn, now, new_seq, session_id),
                )

                payload_max_steps = cont_record.payload.get("max_steps")
                if payload_max_steps is not None and (type(payload_max_steps) is not int or payload_max_steps < 1):
                    raise StoreConflict(f"continuation {cont_record.continuation_id!r} has an invalid max_steps cap")
                if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
                    raise ValueError("max_steps must be a positive integer or null")
                effective_steps = payload_max_steps
                if max_steps is not None:
                    effective_steps = max_steps if effective_steps is None else min(effective_steps, max_steps)
                checkpoint_payload = {
                    "step": 0,
                    "continuation_id": cont_record.continuation_id,
                    "root_id": cont_record.root_id,
                    "logical_turn": logical_turn,
                    "initial_root_budget": initial_root_budget,
                    "max_steps": effective_steps,
                }
                checkpoint_json = canonical_json(checkpoint_payload)
                checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(checkpoint_payload)
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                        root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        revision_id,
                        run_id,
                        new_seq,
                        checkpoint_json,
                        checkpoint_root_id,
                        checkpoint_approval,
                        checkpoint_ask_seq,
                        checkpoint_ask_req,
                        now,
                    ),
                )

                self._connection.execute("COMMIT")
                in_transaction = False
                return cont_record, SessionRecord(
                    session_id=session_id,
                    workspace=session_row.workspace,
                    status=SessionStatus.RUNNING,
                    active_revision_id=revision_id,
                    run_id=run_id,
                    root_session_id=session_row.root_session_id,
                    parent_session_id=session_row.parent_session_id,
                    depth=session_row.depth,
                    tool_allowlist=_decode_tool_allowlist(session_row.tool_allowlist_json),
                    turn=new_turn,
                    last_event_sequence=new_seq,
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def has_observer_receipt(self, session_id: str, event_sequence: int, owner: str) -> bool:
        """Whether this (source fact event, owner) pair was already delivered exactly once."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT 1 FROM observer_receipts
                WHERE session_id = ? AND event_sequence = ? AND owner = ?
                """,
                (session_id, event_sequence, owner),
            )
            return cursor.fetchone() is not None

    def continuation_started(self, session_id: str, continuation_id: str) -> bool:
        """Whether the exact continuation input was committed into this session's event transcript."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM continuation_starts
                WHERE session_id = ? AND continuation_id = ?
                LIMIT 1
                """,
                (session_id, continuation_id),
            ).fetchone()
            return row is not None

    def cancel_pending_continuations(
        self, session_id: str, owner: str, *, continuation_id: str | None = None, expected_session: SessionRecord | None = None
    ) -> int:
        """Cancel pending owned work, optionally pinned to the session's observed run."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE continuations
                SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND owner = ? AND status = 'pending'
                  AND (? IS NULL OR continuation_id = ?)
                  AND (? OR EXISTS (
                      SELECT 1 FROM sessions s WHERE s.session_id = ? AND s.run_id IS ?
                  ))
                """,
                (
                    now,
                    session_id,
                    owner,
                    continuation_id,
                    continuation_id,
                    expected_session is None,
                    session_id,
                    expected_session.run_id if expected_session is not None else None,
                ),
            )
            return cursor.rowcount

    def append_session_event_at_tip(
        self,
        session_id: str,
        *,
        revision_id: str,
        event: EventDraft,
        run_id: str | None = None,
    ) -> StoredEvent:
        """Atomically append one event at the session's current durable tip."""
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                session_row = read_session(self._connection, session_id)
                now = int(time.time() * 1000)
                resolved_run_id = run_id if run_id is not None else (session_row.run_id or "")
                stored = insert_session_event_locked(
                    self._connection,
                    session_id,
                    session_row.last_event_sequence + 1,
                    run_id=resolved_run_id,
                    revision_id=revision_id,
                    draft=event,
                    now=now,
                )
                self._connection.execute(
                    "UPDATE sessions SET updated_at = ?, last_event_sequence = ? WHERE session_id = ?",
                    (now, stored.sequence, session_id),
                )
                self._connection.execute("COMMIT")
                in_transaction = False
                return stored
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
