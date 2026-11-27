"""Durable wakeup scheduling, claiming, and settlement.

A wakeup is an owner's future turn, claimed atomically with its run under the root
authority captured when it was scheduled. Scheduling rejects a lost finite authorization;
claim scans settle structurally dead rows instead of retrying them forever.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import uuid4

from ...sdk.json_values import json_wire_object
from ..events import EventType
from ._database import _ConnectionStore
from ._rows import (
    _checkpoint_index_values,
    _CheckpointGuardColumns,
    _decode_closed_enum,
    _decode_session,
    _decode_wakeup,
    _stored_root_budget,
)
from ._values import (
    MAX_WAKEUP_PAYLOAD_BYTES,
    CheckpointKind,
    EventDraft,
    SessionRecord,
    SessionStatus,
    StoreConflict,
    UnknownSession,
    WakeupRecord,
    WakeupSkip,
    canonical_json,
)
from .writes import event_stop_cause_locked, insert_session_event_locked, interrupted_terminal_status


class _WakeupsMixin(_ConnectionStore):
    """Wakeup scheduling, due-claim scans, and terminal settlement."""

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
        """Claim at most one due wakeup and start it under the root captured when scheduled."""
        claims, _skipped = self.claim_due_wakeups(
            workspace,
            now,
            revision_id=revision_id,
            eligible_owners=eligible_owners,
            limit=1,
            run_id=run_id,
            logical_turn=logical_turn,
        )
        return claims[0] if claims else None

    def claim_due_wakeups(
        self,
        workspace: str,
        now: int,
        *,
        revision_id: str,
        eligible_owners: Sequence[str],
        limit: int | None = None,
        run_id: str | None = None,
        logical_turn: str | None = None,
    ) -> tuple[list[tuple[WakeupRecord, SessionRecord]], list[WakeupSkip]]:
        """Claim every due wakeup whose session admits fresh work, skipping the rest.

        One ``BEGIN IMMEDIATE`` transaction scans due rows in due order and claims
        at most one run per session. A busy, waiting, or checkpoint-resumable
        session is skipped with its pending row and checkpoint untouched; the
        scan continues so one blocked head never starves other due wakeups.
        Structurally dead rows (wrong generation, lost root authorization) are
        settled ``failed`` instead of retried forever. At most ``limit`` runs are
        claimed when a caller asks for one.
        """
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer or null")
        if (run_id is not None or logical_turn is not None) and limit != 1:
            raise ValueError("a caller-supplied run identity requires limit=1")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise ValueError("run_id must be non-empty text")
        if logical_turn is not None and (not isinstance(logical_turn, str) or not logical_turn):
            raise ValueError("logical_turn must be non-empty text")
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                rows = self._connection.execute(
                    """
                    SELECT wakeup_id, owner, workspace, session_id, payload_json, root_id, root_action_budget, role, due_at, status, claim_run_id
                    FROM wakeups
                    WHERE workspace = ? AND status = 'pending' AND due_at <= ?
                    ORDER BY due_at ASC, created_at ASC
                    """,
                    (workspace, now),
                ).fetchall()
                claims: list[tuple[WakeupRecord, SessionRecord]] = []
                skipped: list[WakeupSkip] = []
                claimed_sessions: set[str] = set()
                for row in rows:
                    if limit is not None and len(claims) >= limit:
                        break
                    wakeup = _decode_wakeup(row)
                    session_row = self._connection.execute(
                        """
                        SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                               parent_session_id, root_session_id, depth, tool_allowlist_json
                        FROM sessions WHERE session_id = ?
                        """,
                        (wakeup.session_id,),
                    ).fetchone()
                    if session_row is None:
                        self._fail_pending_wakeup_locked(wakeup.wakeup_id, now)
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "unknown_session"))
                        continue
                    session = _decode_session(session_row)
                    if wakeup.session_id in claimed_sessions:
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "session_already_claimed"))
                        continue
                    if session.active_revision_id != revision_id:
                        self._fail_pending_wakeup_locked(wakeup.wakeup_id, now)
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "revision_mismatch"))
                        continue
                    if session.status is SessionStatus.RUNNING:
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "session_running"))
                        continue
                    if session.status is SessionStatus.WAITING:
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "session_parked"))
                        continue
                    checkpoint_row = self._connection.execute(
                        "SELECT kind, run_id, payload_json FROM checkpoints WHERE session_id = ?",
                        (wakeup.session_id,),
                    ).fetchone()
                    checkpoint_kind: CheckpointKind | None = None
                    checkpoint_run_id: str | None = None
                    if checkpoint_row is not None:
                        guard = _CheckpointGuardColumns(*checkpoint_row)
                        checkpoint_run_id = guard.run_id
                        checkpoint_kind = _decode_closed_enum(
                            CheckpointKind, guard.kind, field="kind", record=f"checkpoint of session {wakeup.session_id!r}"
                        )
                    if checkpoint_kind is not None and checkpoint_kind is not CheckpointKind.TERMINAL:
                        # A resumable/native-batch cursor is owned by an explicit resume, never
                        # overwritten by a fresh scheduled turn.
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, f"checkpoint_{checkpoint_kind.value}"))
                        continue
                    if session.status is SessionStatus.INTERRUPTED and (
                        checkpoint_kind is not CheckpointKind.TERMINAL or checkpoint_run_id != session.run_id
                    ):
                        # Mirrors admits_fresh_work: only a terminal interruption of the
                        # session's own run is closed work; anything else needs resume.
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "session_interrupted"))
                        continue
                    budget_row = self._connection.execute(
                        "SELECT budget FROM root_budgets WHERE root_id = ?",
                        (wakeup.root_id,),
                    ).fetchone()
                    stored_budget = _stored_root_budget(budget_row)
                    if wakeup.root_action_budget is None and stored_budget is not None:
                        self._fail_pending_wakeup_locked(wakeup.wakeup_id, now)
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "root_authorization"))
                        continue
                    if wakeup.root_action_budget is not None and (stored_budget is None or stored_budget < wakeup.root_action_budget):
                        self._fail_pending_wakeup_locked(wakeup.wakeup_id, now)
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "root_authorization"))
                        continue
                    initial_root_budget = wakeup.root_action_budget
                    claim_run_id = run_id or uuid4().hex
                    claimed_logical_turn = logical_turn or uuid4().hex
                    claimed = self._connection.execute(
                        """
                        UPDATE wakeups SET status = 'claimed', claim_run_id = ?, updated_at = ?
                        WHERE wakeup_id = ? AND status = 'pending'
                        """,
                        (claim_run_id, now, wakeup.wakeup_id),
                    )
                    if claimed.rowcount == 0:
                        skipped.append(WakeupSkip(wakeup.wakeup_id, wakeup.session_id, "claimed_concurrently"))
                        continue
                    new_seq = session.last_event_sequence + 1
                    insert_session_event_locked(
                        self._connection,
                        session.session_id,
                        new_seq,
                        run_id=claim_run_id,
                        revision_id=revision_id,
                        draft=EventDraft(
                            EventType.WAKEUP_FIRED,
                            f"plugin.{wakeup.owner}",
                            "plugin",
                            {
                                "eligible_owners": list(eligible_owners),
                                "wakeup_id": wakeup.wakeup_id,
                                "owner": wakeup.owner,
                                "payload": json_wire_object(wakeup.payload),
                                "role": wakeup.role,
                                "logical_turn": claimed_logical_turn,
                            },
                        ),
                        now=now,
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
                        "logical_turn": claimed_logical_turn,
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
                            claim_run_id,
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
                        (claim_run_id, now, new_seq, session.session_id),
                    )
                    claimed_sessions.add(session.session_id)
                    claims.append(
                        (
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
                                claim_run_id=claim_run_id,
                            ),
                            SessionRecord(
                                session_id=session.session_id,
                                workspace=session.workspace,
                                status=SessionStatus.RUNNING,
                                active_revision_id=revision_id,
                                run_id=claim_run_id,
                                turn=session.turn,
                                last_event_sequence=new_seq,
                                root_session_id=session.root_session_id,
                                parent_session_id=session.parent_session_id,
                                depth=session.depth,
                                tool_allowlist=session.tool_allowlist,
                            ),
                        )
                    )
                self._connection.execute("COMMIT")
                in_transaction = False
                return claims, skipped
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _fail_pending_wakeup_locked(self, wakeup_id: str, now: int) -> None:
        """Settle a structurally dead pending wakeup inside the caller's transaction."""
        self._connection.execute(
            "UPDATE wakeups SET status = 'failed', updated_at = ? WHERE wakeup_id = ? AND status = 'pending'",
            (now, wakeup_id),
        )

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
                            try:
                                parsed_cp = json.loads(checkpoint_json)
                            except TypeError, ValueError:
                                parsed_cp = None
                            if isinstance(parsed_cp, Mapping):
                                checkpoint_payload = parsed_cp
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

    def wakeup_status(self, wakeup_id: str) -> str | None:
        """Return one wakeup row's durable status, or None when the identity is unused."""
        with self._lock:
            self._check_open()
            row = self._connection.execute("SELECT status FROM wakeups WHERE wakeup_id = ?", (wakeup_id,)).fetchone()
            return None if row is None else str(row[0])

    def abandon_claimed_wakeup(
        self,
        wakeup_id: str,
        *,
        reason: str,
        eligible_owners: Sequence[str] = (),
    ) -> str | None:
        """Settle a claimed wakeup whose run failed outside the turn terminal path.

        The runtime calls this when driving or delivering a claimed wakeup raises before
        a terminal event commits. It appends exactly one ``run_interrupted`` at the
        session's last committed boundary and settles the claim, so the next ticker or
        startup replay cannot leave a session running with a forgotten claim. A parked
        (waiting) run keeps its claim; a claim owned by another run is left alone.
        Returns the settled wakeup status, or None when this call did not own the claim.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty text")
        reason_text = reason[:1000]
        owners = tuple(dict.fromkeys(str(owner) for owner in eligible_owners))
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                wakeup_row = self._connection.execute(
                    "SELECT session_id, status, claim_run_id FROM wakeups WHERE wakeup_id = ?",
                    (wakeup_id,),
                ).fetchone()
                if wakeup_row is None:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                session_id, wakeup_status_value, claim_run_id = wakeup_row
                if wakeup_status_value != "claimed":
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                session_row = self._connection.execute(
                    "SELECT status, run_id, last_event_sequence, active_revision_id FROM sessions WHERE session_id = ?",
                    (str(session_id),),
                ).fetchone()
                if session_row is None:
                    raise StoreConflict(f"claimed wakeup {wakeup_id!r} names unknown session {session_id!r}")
                raw_status, run_id, last_sequence, revision_id = session_row
                session_status = _decode_closed_enum(SessionStatus, str(raw_status), field="status", record=f"session {session_id!r}")
                if session_status is SessionStatus.WAITING:
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                if claim_run_id is None or run_id is None or str(claim_run_id) != str(run_id):
                    # A newer run owns the session; this claim is not this caller's to settle.
                    self._connection.execute("COMMIT")
                    in_transaction = False
                    return None
                now = int(time.time() * 1000)
                latest = self._connection.execute(
                    "SELECT event_type, run_id FROM session_events WHERE session_id = ? AND sequence = ?",
                    (str(session_id), int(last_sequence)),
                ).fetchone()
                terminal_status: Literal["completed", "failed", "cancelled"] | None = None
                checkpoint_kind: CheckpointKind | None = None
                if latest is not None and str(latest[1]) == str(run_id):
                    if latest[0] == EventType.RUN_COMPLETED:
                        terminal_status, checkpoint_kind = "completed", CheckpointKind.TERMINAL
                    elif latest[0] == EventType.RUN_FAILED:
                        terminal_status, checkpoint_kind = "failed", CheckpointKind.TERMINAL
                    elif latest[0] == EventType.RUN_INTERRUPTED:
                        terminal_status = interrupted_terminal_status(event_stop_cause_locked(self._connection, str(session_id), int(last_sequence)))
                        checkpoint_kind = CheckpointKind.TERMINAL if terminal_status == "cancelled" else CheckpointKind.INTERRUPTED
                if terminal_status is not None:
                    # The run's own terminal event committed; only the claim/session row lagged.
                    settled_status = terminal_status
                    session_status_after = (
                        SessionStatus.COMPLETED
                        if terminal_status == "completed"
                        else SessionStatus.FAILED
                        if terminal_status == "failed"
                        else SessionStatus.INTERRUPTED
                    )
                    self._connection.execute(
                        "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                        (session_status_after.value, now, str(session_id)),
                    )
                    if checkpoint_kind is not None:
                        self._connection.execute(
                            "UPDATE checkpoints SET kind = ?, updated_at = ? WHERE session_id = ?",
                            (checkpoint_kind.value, now, str(session_id)),
                        )
                elif session_status is SessionStatus.RUNNING:
                    logical_turn = ""
                    checkpoint = self._connection.execute(
                        "SELECT payload_json FROM checkpoints WHERE session_id = ?",
                        (str(session_id),),
                    ).fetchone()
                    if checkpoint is not None:
                        try:
                            checkpoint_payload = json.loads(checkpoint[0])
                        except TypeError, ValueError:
                            checkpoint_payload = None
                        if isinstance(checkpoint_payload, Mapping) and isinstance(checkpoint_payload.get("logical_turn"), str):
                            logical_turn = checkpoint_payload["logical_turn"]
                    sequence = int(last_sequence) + 1
                    insert_session_event_locked(
                        self._connection,
                        str(session_id),
                        sequence,
                        run_id=str(run_id),
                        revision_id=str(revision_id),
                        draft=EventDraft(
                            EventType.RUN_INTERRUPTED,
                            "core",
                            "core",
                            {
                                "reason": reason_text,
                                "stop_cause": "error",
                                "logical_turn": logical_turn,
                                "eligible_owners": list(owners),
                            },
                        ),
                        now=now,
                    )
                    self._connection.execute(
                        "UPDATE sessions SET status = 'interrupted', last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                        (sequence, now, str(session_id)),
                    )
                    self._connection.execute(
                        "UPDATE checkpoints SET kind = 'interrupted', last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                        (sequence, now, str(session_id)),
                    )
                    settled_status = "failed"
                else:
                    settled_status = "completed" if session_status is SessionStatus.COMPLETED else "failed"
                self._connection.execute(
                    "UPDATE wakeups SET status = ?, updated_at = ? WHERE wakeup_id = ? AND status = 'claimed'",
                    (settled_status, now, wakeup_id),
                )
                self._connection.execute("COMMIT")
                in_transaction = False
                return settled_status
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _event_stop_cause(self, session_id: str, sequence: int) -> object:
        """The recorded stop cause for one interrupted event inside the caller's transaction."""
        return event_stop_cause_locked(self._connection, session_id, sequence)

    def settle_claimed_wakeups(self, session_id: str, status: Literal["completed", "failed", "cancelled"]) -> int:
        """Settle every claimed wakeup of one session whose run just finished."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "UPDATE wakeups SET status = ?, updated_at = ? WHERE session_id = ? AND status = 'claimed'",
                (status, int(time.time() * 1000), session_id),
            )
            return cursor.rowcount
