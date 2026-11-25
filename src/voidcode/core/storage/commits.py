"""Atomic session commits: CAS, append-only events, staged work, and checkpoint upsert.

`commit_staged` is the one transaction every run boundary uses. It re-verifies the
caller's sequence/run/revision snapshot, applies staged state/continuation/budget/
confirmation/wakeup/receipt work in recorded order, appends events, upserts the
checkpoint with real appended sequences, and settles the session claim in one
`BEGIN IMMEDIATE` transaction.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence
from typing import Literal

from ..events import EventType
from ._database import _ConnectionStore
from ._rows import (
    _checkpoint_index_values,
    _continuation_kind,
    _decode_tool_allowlist,
    _RootBudgetColumns,
    _SessionColumns,
    _stored_root_budget,
)
from ._values import (
    BACKGROUND_ACTION_SOURCE,
    MAX_CONTINUATION_PAYLOAD_BYTES,
    MAX_WAKEUP_PAYLOAD_BYTES,
    AskAnswerConflict,
    CheckpointKind,
    ContinuationStatus,
    EventDraft,
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
    UnknownSession,
    _canonical_json_value,
    _int_value,
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


class _CommitsMixin(_ConnectionStore):
    """Atomic CAS commits over the session's event stream and durable checkpoint."""

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
