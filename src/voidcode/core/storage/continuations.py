"""Continuation/background-action storage domain.

Wakeups, model continuations, background actions, root-action budgets, and child-message claims.

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
from typing import Literal
from uuid import uuid4

from ...sdk.json_values import json_wire_object, own_json_object
from ..events import CHILD_MESSAGE_OWNER, EventType
from .records import (
    BACKGROUND_ACTION_SOURCE,
    MAX_CONTINUATION_PAYLOAD_BYTES,
    MAX_WAKEUP_PAYLOAD_BYTES,
    CheckpointKind,
    ContinuationRecord,
    ContinuationStatus,
    EventDraft,
    FencedSubtree,
    PendingFollowupRecord,
    SessionRecord,
    SessionStatus,
    StaleRevision,
    StoreConflict,
    StoredEvent,
    UnknownContinuation,
    UnknownSession,
    WakeupRecord,
    _BackgroundActionColumns,
    _checkpoint_index_values,
    _CheckpointGuardColumns,
    _ChildClaimGuardColumns,
    _ConnectionStore,
    _continuation_kind,
    _ContinuationColumns,
    _decode_background_action,
    _decode_closed_enum,
    _decode_continuation,
    _decode_session,
    _decode_tool_allowlist,
    _decode_wakeup,
    _RootBudgetColumns,
    _SessionColumns,
    _stored_root_budget,
    canonical_json,
)
from .writes import insert_session_event_locked, read_session, record_event_indexes


def _decode_followup_payload(cid: str, payload_json: str) -> tuple[str, int | None, list[dict[str, object]], Mapping[str, object]]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise StoreConflict(f"queued follow-up {cid!r} has invalid JSON payload") from exc
    if not isinstance(payload, Mapping):
        raise StoreConflict(f"queued follow-up {cid!r} has invalid payload type")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise StoreConflict(f"queued follow-up {cid!r} has no prompt")
    max_steps_value = payload.get("max_steps")
    if max_steps_value is not None and type(max_steps_value) is not int:
        raise StoreConflict(f"queued follow-up {cid!r} has an invalid provider-turn cap")
    raw_images = payload.get("images")
    images: list[dict[str, object]] = [dict(img) for img in raw_images if isinstance(img, Mapping)] if isinstance(raw_images, (list, tuple)) else []
    return prompt, max_steps_value, images, payload


class _ContinuationsMixin(_ConnectionStore):
    """Queued work: wakeups, continuations, background actions, root action budgets, child-message claims."""

    def claim_and_start_wakeup(
        self,
        workspace: str,
        now: int,
        *,
        run_id: str,
        logical_turn: str,
        revision_id: str,
        eligible_owners: Sequence[str],
    ) -> tuple[WakeupRecord, SessionRecord] | None:
        """Claim a due wakeup and start it under the root identity captured when scheduled."""
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                cursor = self._connection.execute(
                    """
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget, role, due_at, status, claim_run_id
                    FROM wakeups
                    WHERE workspace = ? AND status = 'pending' AND due_at <= ?
                    ORDER BY due_at ASC, created_at ASC
                    LIMIT 1
                    """,
                    (workspace, now),
                )
                row = cursor.fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                wakeup = _decode_wakeup(row)
                claimed = self._connection.execute(
                    """
                    UPDATE wakeups SET status = 'claimed', claim_run_id = ?, updated_at = ?
                    WHERE wakeup_id = ? AND status = 'pending'
                    """,
                    (run_id, now, wakeup.wakeup_id),
                )
                if claimed.rowcount == 0:
                    raise StoreConflict(f"wakeup {wakeup.wakeup_id!r} was claimed concurrently")
                session_row = self._connection.execute(
                    """
                    SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                           parent_session_id, root_session_id, depth, tool_allowlist_json
                    FROM sessions WHERE session_id = ?
                    """,
                    (wakeup.session_id,),
                ).fetchone()
                if session_row is None:
                    raise UnknownSession(f"wakeup {wakeup.wakeup_id!r} names an unknown session", wakeup.session_id)
                session = _decode_session(session_row)
                if session.active_revision_id != revision_id:
                    raise StoreConflict("wakeup session belongs to another capability revision")
                if session.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
                    raise StoreConflict(f"wakeup session {session.session_id!r} is busy ({session.status.value})")
                budget_row = self._connection.execute(
                    "SELECT budget FROM root_budgets WHERE root_id = ?",
                    (wakeup.root_id,),
                ).fetchone()
                stored_budget = _stored_root_budget(budget_row)
                if wakeup.root_action_budget is None and stored_budget is not None:
                    raise StoreConflict("wakeup captured an uncapped root with a finite budget row")
                if wakeup.root_action_budget is not None and (stored_budget is None or stored_budget < wakeup.root_action_budget):
                    raise StoreConflict("wakeup lost its captured finite root authorization")
                initial_root_budget = wakeup.root_action_budget
                new_seq = session.last_event_sequence + 1
                fired_payload = canonical_json(
                    {
                        "eligible_owners": list(eligible_owners),
                        "wakeup_id": wakeup.wakeup_id,
                        "owner": wakeup.owner,
                        "payload": json_wire_object(wakeup.payload),
                        "role": wakeup.role,
                        "logical_turn": logical_turn,
                    }
                )
                self._connection.execute(
                    """
                    INSERT INTO session_events (
                        session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                    ) VALUES (?, ?, ?, ?, ?, 'plugin', ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        new_seq,
                        run_id,
                        EventType.WAKEUP_FIRED,
                        f"plugin.{wakeup.owner}",
                        revision_id,
                        fired_payload,
                        len(fired_payload.encode("utf-8")),
                    ),
                )
                wakeup_checkpoint = {
                    "batch_sequence": None,
                    "next_call": 0,
                    "intent_sequence": None,
                    "approval_sequence": None,
                    "deferred_sequence": None,
                    "deferred_depth": 0,
                    "deferred_only": False,
                    "max_steps": None,
                    "step": 0,
                    "abandon_batch": False,
                    "continuation_id": None,
                    "logical_turn": logical_turn,
                    "stop_cause": None,
                    "initial_root_budget": initial_root_budget,
                    "root_id": wakeup.root_id,
                    "resume_intent": "",
                }
                checkpoint_payload = canonical_json(wakeup_checkpoint)
                checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(wakeup_checkpoint)
                self._connection.execute(
                    """
                    INSERT INTO checkpoints (
                        session_id, kind, revision_id, run_id, last_event_sequence, payload_json,
                        root_id, approval_sequence, ask_sequence, ask_request_id, updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET
                        kind = 'ready',
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
                        session.session_id,
                        revision_id,
                        run_id,
                        new_seq,
                        checkpoint_payload,
                        checkpoint_root_id,
                        checkpoint_approval,
                        checkpoint_ask_seq,
                        checkpoint_ask_req,
                        now,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'running', run_id = ?, updated_at = ?, last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (run_id, now, new_seq, session.session_id),
                )
                self._connection.execute("COMMIT")
                in_transaction = False
                return (
                    WakeupRecord(
                        wakeup_id=wakeup.wakeup_id,
                        owner=wakeup.owner,
                        workspace=wakeup.workspace,
                        session_id=wakeup.session_id,
                        payload=wakeup.payload,
                        root_id=wakeup.root_id,
                        role=wakeup.role,
                        due_at=wakeup.due_at,
                        status="claimed",
                        claim_run_id=run_id,
                    ),
                    SessionRecord(
                        session_id=session.session_id,
                        workspace=session.workspace,
                        status=SessionStatus.RUNNING,
                        active_revision_id=revision_id,
                        run_id=run_id,
                        turn=session.turn,
                        last_event_sequence=new_seq,
                        root_session_id=session.root_session_id,
                        parent_session_id=session.parent_session_id,
                        depth=session.depth,
                        tool_allowlist=session.tool_allowlist,
                    ),
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def schedule_wakeup(
        self,
        wakeup_id: str,
        owner: str,
        workspace: str,
        session_id: str,
        payload: Mapping[str, object],
        due_at: int,
        role: str = "system",
        *,
        root_action_id: str | None = None,
        root_action_budget: int | None = None,
    ) -> str:
        """Persist a wakeup with the root authorization captured by its caller."""
        if root_action_id is not None and (not isinstance(root_action_id, str) or not root_action_id or "\0" in root_action_id):
            raise ValueError("root_action_id must be nonempty text without NUL")
        if root_action_budget is not None and (type(root_action_budget) is not int or root_action_budget < 0):
            raise ValueError("root_action_budget must be a non-negative integer or null")
        if root_action_id is None and root_action_budget is not None:
            raise ValueError("a finite root_action_budget requires its captured root_action_id")
        wakeup_size = len(canonical_json(payload).encode("utf-8"))
        if wakeup_size > MAX_WAKEUP_PAYLOAD_BYTES:
            raise StoreConflict(
                f"wakeup {wakeup_id!r} payload is {wakeup_size} bytes, exceeding the {MAX_WAKEUP_PAYLOAD_BYTES}-byte wakeup-payload bound"
            )
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                session = self._connection.execute(
                    "SELECT status FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise UnknownSession(f"wakeup session {session_id!r} does not exist", session_id)
                (session_status,) = session
                if root_action_id is None:
                    if _decode_closed_enum(SessionStatus, session_status, field="status", record=f"session {session_id!r}") in (
                        SessionStatus.RUNNING,
                        SessionStatus.WAITING,
                    ):
                        checkpoint = self._connection.execute(
                            "SELECT payload_json FROM checkpoints WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        checkpoint_payload = None
                        if checkpoint is not None:
                            (checkpoint_json,) = checkpoint
                            checkpoint_payload = json.loads(checkpoint_json)
                        candidate_root_id = None if checkpoint_payload is None else checkpoint_payload.get("root_id")
                        if not isinstance(candidate_root_id, str) or not candidate_root_id:
                            raise StoreConflict(f"active wakeup session {session_id!r} has no captured root action identity")
                        root_action_id = candidate_root_id
                        if checkpoint_payload is None or "initial_root_budget" not in checkpoint_payload:
                            raise StoreConflict(f"active wakeup session {session_id!r} has no captured root budget")
                        candidate_budget = checkpoint_payload.get("initial_root_budget")
                        if candidate_budget is None:
                            root_action_budget = None
                        elif type(candidate_budget) is int and candidate_budget >= 0:
                            root_action_budget = candidate_budget
                        else:
                            raise StoreConflict(f"active wakeup session {session_id!r} has an invalid captured root budget")
                    else:
                        root_action_id = uuid4().hex
                        root_action_budget = None
                budget_row = self._connection.execute(
                    "SELECT budget FROM root_budgets WHERE root_id = ?",
                    (root_action_id,),
                ).fetchone()
                stored_budget = _stored_root_budget(budget_row)
                if root_action_budget is None and stored_budget is not None:
                    raise StoreConflict("wakeup captured an uncapped root with a finite budget row")
                if root_action_budget is not None and (stored_budget is None or stored_budget < root_action_budget):
                    raise StoreConflict("wakeup lost its captured finite root authorization")
                self._connection.execute(
                    """
                    INSERT INTO wakeups (
                        wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget,
                        role, due_at, status, claim_run_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                    ON CONFLICT (wakeup_id) DO NOTHING
                    """,
                    (
                        wakeup_id,
                        owner,
                        workspace,
                        session_id,
                        canonical_json(payload),
                        root_action_id,
                        root_action_budget,
                        role if role in ("system", "user") else "system",
                        due_at,
                        now,
                        now,
                    ),
                )
                self._connection.execute("COMMIT")
                return wakeup_id
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def cancel_wakeup(self, wakeup_id: str, owner: str) -> bool:
        """Cancel one pending wakeup of this owner; already claimed wakeups are not touched."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "UPDATE wakeups SET status = 'cancelled', updated_at = ? WHERE wakeup_id = ? AND owner = ? AND status = 'pending'",
                (int(time.time() * 1000), wakeup_id, owner),
            )
            return cursor.rowcount > 0

    def complete_wakeup(self, wakeup_id: str, status: Literal["completed", "failed", "cancelled"] = "completed") -> None:
        """Settle a claimed wakeup once its run reached a terminal state."""
        with self._lock:
            self._check_open()
            self._connection.execute(
                "UPDATE wakeups SET status = ?, updated_at = ? WHERE wakeup_id = ? AND status = 'claimed'",
                (status, int(time.time() * 1000), wakeup_id),
            )

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

    def has_pending_user_message(self, session_id: str) -> bool:
        """Whether fresh user input is queued behind this session's active run."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT 1 FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = 'core' AND source = 'user_message'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return row is not None

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

    def enqueue_background_action(
        self,
        action_id: str,
        owner: str,
        session_id: str,
        revision_id: str,
        action: Mapping[str, object],
        *,
        root_id: str,
        root_action_budget: int | None = None,
        authority: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Queue one background action with its captured root authorization (direct insert)."""
        if not isinstance(root_id, str) or not root_id:
            raise StoreConflict(f"background action {action_id!r} has no captured root authorization")
        if root_action_budget is not None and (type(root_action_budget) is not int or not 0 <= root_action_budget <= 9_223_372_036_854_775_807):
            raise StoreConflict(f"background action {action_id!r} has an invalid captured root budget")
        action_json = canonical_json(action)
        action_size = len(action_json.encode("utf-8"))
        if action_size > MAX_CONTINUATION_PAYLOAD_BYTES:
            raise StoreConflict(
                f"background action {action_id!r} payload is {action_size} bytes, "
                f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
            )
        authority_json = None if authority is None else canonical_json(authority)
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            if idempotency_key is not None:
                row = self._connection.execute(
                    """
                    SELECT action_id, root_id, payload_json, authority_json, root_action_budget FROM background_actions
                    WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                    """,
                    (session_id, owner, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing_id, existing_root_id, existing_action_json, existing_authority_json, existing_budget = row
                    if (
                        existing_root_id != root_id
                        or existing_action_json != action_json
                        or existing_authority_json != authority_json
                        or existing_budget != root_action_budget
                    ):
                        raise StoreConflict("background action idempotency key was already used with a different authorization or payload")
                    return existing_id
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
                        action_id,
                        owner,
                        session_id,
                        revision_id,
                        idempotency_key,
                        root_id,
                        root_action_budget,
                        action_json,
                        authority_json,
                        now,
                        now,
                    ),
                )
                return action_id
            except sqlite3.IntegrityError as exc:
                raise StoreConflict(f"background action {action_id!r} already exists") from exc

    def claim_background_action(self, *, workspace: str, claim_identity: Mapping[str, object]) -> ContinuationRecord | None:
        """Claim ONE pending background action of this workspace; its driver runs it, not a model turn.

        The `claim_identity` records who holds the claim in the claim column; the
        action payload itself is never rewritten by the claim.
        """
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                row = self._connection.execute(
                    """
                    SELECT action_id, owner, session_id, revision_id, idempotency_key, root_id,
                           root_action_budget, status, payload_json, authority_json, claim_json, error,
                           created_at, updated_at
                    FROM background_actions
                    WHERE status = 'pending'
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (workspace,),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                now = int(time.time() * 1000)
                action_row = _BackgroundActionColumns(*row)
                if not action_row.root_id:
                    raise StoreConflict(f"background action {action_row.action_id!r} has no captured root authorization")
                root_budget = action_row.root_action_budget
                if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
                    raise StoreConflict(f"background action {action_row.action_id!r} has an invalid captured root budget")
                budget_row = self._connection.execute(
                    "SELECT budget FROM root_budgets WHERE root_id = ?",
                    (action_row.root_id,),
                ).fetchone()
                stored_budget = _stored_root_budget(budget_row)
                if root_budget is None and stored_budget is not None:
                    raise StoreConflict(f"background action {action_row.action_id!r} captured an uncapped root with a finite budget row")
                if root_budget is not None and (stored_budget is None or stored_budget < root_budget):
                    raise StoreConflict(f"background action {action_row.action_id!r} lost its captured finite root authorization")
                claimed = self._connection.execute(
                    """
                    UPDATE background_actions SET status = 'claimed', claim_json = ?, updated_at = ?
                    WHERE action_id = ? AND status = 'pending'
                    """,
                    (canonical_json(claim_identity), now, action_row.action_id),
                )
                if claimed.rowcount == 0:
                    raise StoreConflict(f"background action {action_row.action_id!r} was claimed concurrently")
                self._connection.execute("COMMIT")
                in_transaction = False
                return _decode_background_action(row, status=ContinuationStatus.CLAIMED)
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def complete_background_action(self, action_id: str, *, status: ContinuationStatus, error: str | None = None) -> None:
        """Settle a pending or claimed background action into its terminal state.

        A failed settle records the driver's error in the error column, leaving the
        owner's action payload byte-identical: business payloads are arbitrary JSON
        and must never be rewritten or misread as execution errors.
        """
        with self._lock:
            self._check_open()
            if status not in (ContinuationStatus.COMPLETED, ContinuationStatus.FAILED, ContinuationStatus.CANCELLED):
                raise ValueError(f"background action completion must be terminal, got {status.value!r}")
            cursor = self._connection.execute(
                """
                UPDATE background_actions SET status = ?, updated_at = ?, error = ?
                WHERE action_id = ? AND status IN ('pending', 'claimed')
                """,
                (status.value, int(time.time() * 1000), error if status is ContinuationStatus.FAILED else None, action_id),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"background action {action_id!r} is not settleable")

    def background_action(self, action_id: str) -> ContinuationRecord | None:
        """One background action row by id, or None when absent."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT action_id, owner, session_id, revision_id, idempotency_key, root_id,
                       root_action_budget, status, payload_json, authority_json, claim_json, error,
                       created_at, updated_at
                FROM background_actions
                WHERE action_id = ?
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                return None
            return _decode_background_action(row)

    def background_action_id_for_idempotency(self, session_id: str, owner: str, idempotency_key: str) -> str | None:
        """The durable background action id already recorded for an idempotency key."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT action_id FROM background_actions
                WHERE session_id = ? AND owner = ? AND idempotency_key = ?
                """,
                (session_id, owner, idempotency_key),
            ).fetchone()
            return None if row is None else row[0]

    def pending_background_actions(self, session_id: str) -> tuple[ContinuationRecord, ...]:
        """Every pending or claimed background action of one session, oldest first."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT action_id, owner, session_id, revision_id, idempotency_key, root_id,
                       root_action_budget, status, payload_json, authority_json, claim_json, error,
                       created_at, updated_at
                FROM background_actions
                WHERE session_id = ? AND status IN ('pending', 'claimed')
                ORDER BY created_at ASC
                LIMIT 64
                """,
                (session_id,),
            ).fetchall()
            return tuple(_decode_background_action(row) for row in rows)

    def pending_followups(self, session_id: str, *, limit: int = 64) -> tuple[PendingFollowupRecord, ...]:
        """Pending user follow-ups for one session in FIFO execution order."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                """
                SELECT continuation_id, revision_id, payload_json, created_at
                FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = 'core' AND source = 'user_message'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, max(1, int(limit))),
            ).fetchall()
            records: list[PendingFollowupRecord] = []
            for row in rows:
                cid = str(row[0])
                prompt, steps, images, _ = _decode_followup_payload(cid, str(row[2]))
                records.append(
                    PendingFollowupRecord(
                        continuation_id=cid,
                        prompt=prompt,
                        revision_id=str(row[1]),
                        created_at=int(row[3]),
                        max_steps=steps,
                        image_count=len(images),
                    )
                )
            return tuple(records)

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

    def withdraw_pending_followup(
        self,
        session_id: str,
        continuation_id: str,
        *,
        expected_revision: str,
        revision_id: str,
    ) -> PendingFollowupRecord:
        """Atomically withdraw a pending user follow-up before claim and record the withdrawal event."""
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                session_row = read_session(self._connection, session_id)
                if session_row.active_revision_id != expected_revision:
                    raise StaleRevision(
                        f"displayed catalog revision {expected_revision} is stale; refresh the catalog",
                        expected_revision,
                        session_row.active_revision_id,
                    )
                row = self._connection.execute(
                    """
                    SELECT owner, session_id, source, revision_id, status, payload_json, created_at
                    FROM continuations
                    WHERE continuation_id = ?
                    """,
                    (continuation_id,),
                ).fetchone()
                if row is None or row[0] != "core" or row[1] != session_id or row[2] != "user_message":
                    raise UnknownContinuation(f"unknown follow-up: {continuation_id}", continuation_id)
                cont_revision, cont_status, payload_json, created_at = str(row[3]), str(row[4]), str(row[5]), int(row[6])
                if cont_status != "pending":
                    raise StoreConflict(f"follow-up {continuation_id!r} cannot be withdrawn in state {cont_status!r}")
                prompt, steps, images, _ = _decode_followup_payload(continuation_id, payload_json)
                now = int(time.time() * 1000)
                updated = self._connection.execute(
                    """
                    UPDATE continuations
                    SET status = 'cancelled', updated_at = ?
                    WHERE continuation_id = ? AND session_id = ? AND owner = 'core' AND source = 'user_message' AND status = 'pending'
                    """,
                    (now, continuation_id, session_id),
                )
                if updated.rowcount == 0:
                    raise StoreConflict(f"follow-up {continuation_id!r} was already claimed or cancelled")
                event_payload: dict[str, object] = {
                    "continuation_id": continuation_id,
                    "prompt": prompt,
                    "image_count": len(images),
                    "reason": "withdrawn",
                }
                if steps is not None:
                    event_payload["max_steps"] = steps
                stored = insert_session_event_locked(
                    self._connection,
                    session_id,
                    session_row.last_event_sequence + 1,
                    run_id=session_row.run_id or continuation_id,
                    revision_id=revision_id,
                    draft=EventDraft(EventType.FOLLOWUP_WITHDRAWN, "core", "user", event_payload),
                    now=now,
                )
                self._connection.execute(
                    "UPDATE sessions SET updated_at = ?, last_event_sequence = ? WHERE session_id = ?",
                    (now, stored.sequence, session_id),
                )
                self._connection.execute("COMMIT")
                in_transaction = False
                return PendingFollowupRecord(
                    continuation_id=continuation_id,
                    prompt=prompt,
                    revision_id=cont_revision,
                    created_at=created_at,
                    max_steps=steps,
                    image_count=len(images),
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def claim_pending_followup(
        self,
        session_id: str,
    ) -> tuple[str, str, int | None, list[dict[str, object]], str | None, int | None] | None:
        """Atomically claim a core follow-up with its captured root authorization."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT continuation_id, payload_json, root_id
                FROM continuations
                WHERE session_id = ? AND status = 'pending' AND owner = 'core' AND source = 'user_message'
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cid, payload_json, root_id = row
            prompt, steps, images, payload = _decode_followup_payload(str(cid), str(payload_json))
            if "root_action_budget" not in payload:
                raise StoreConflict(f"queued follow-up {cid!r} has no captured root budget")
            if not isinstance(root_id, str) or not root_id:
                raise StoreConflict(f"queued follow-up {cid!r} has no captured root action identity")
            root_budget = payload["root_action_budget"]
            if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
                raise StoreConflict(f"queued follow-up {cid!r} has an invalid captured root budget")
            now = int(time.time() * 1000)
            claimed = self._connection.execute(
                "UPDATE continuations SET status = 'claimed', updated_at = ? WHERE continuation_id = ? AND status = 'pending'",
                (now, cid),
            )
            if claimed.rowcount == 0:
                raise StoreConflict(f"queued follow-up {cid!r} was claimed concurrently")
            return cid, prompt, steps, images, root_id, root_budget

    def set_continuation_run_id(self, continuation_id: str, run_id: str) -> None:
        """Record the active run_id for a claimed continuation."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            self._connection.execute(
                "UPDATE continuations SET run_id = ?, updated_at = ? WHERE continuation_id = ?",
                (run_id, now, continuation_id),
            )
