"""Activation/state storage domain.

Capability revisions, the workspace pointer, adoption/activation, owner state, confirmations, budgets, overlays, and projections.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`. Public methods stay on the
facade via inheritance, so `store.<method>` addresses the same one store object.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from uuid import uuid4

from ...sdk.hooks import ObservedFactKind
from ...sdk.json_values import own_json_object
from ...sdk.plugin import StateScope
from ...sdk.services import PluginBudget
from ..events import EventType
from .records import (
    ActionConfirmationRecord,
    CheckpointKind,
    ConfirmationStatus,
    EventDraft,
    PluginStateRow,
    SequenceConflict,
    SessionRecord,
    SessionStatus,
    StagedStateUpdate,
    StorageError,
    StoreConflict,
    UnknownConfirmation,
    UnknownSession,
    WindowProjectionRecord,
    _AdoptSessionColumns,
    _checkpoint_index_values,
    _ConnectionStore,
    _decode_budget,
    _decode_closed_enum,
    _decode_confirmation,
    _decode_plugin_state,
    _decode_projection,
    _decode_tool_allowlist,
    _int_value,
    _SessionColumns,
    canonical_json,
)
from .writes import apply_state_updates_locked, read_session, record_event_indexes, set_window_projection_locked


class _StateMixin(_ConnectionStore):
    """Activation and owner state: revisions, workspace pointer, overlays/projections, adoption, plugin state, confirmations, budgets."""

    def put_revision(self, payload: Mapping[str, object], *, revision_id: str | None = None) -> str:
        """Persist one generation payload under an opaque id and return that id.

        A generation id names a composition/config snapshot, not source bytes; callers that
        already own an id (an admission, a pinned approval target) pass it so the same row is
        addressed before and after the host finalized its runtime metadata.
        """
        canonical_text = canonical_json(payload)
        revision_id = uuid4().hex if revision_id is None else revision_id
        if not revision_id:
            raise ValueError("revision id must be a non-empty string")
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            existing = self._connection.execute(
                "SELECT payload_json FROM revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if existing is not None:
                (existing_json,) = existing
                if existing_json != canonical_text:
                    raise ValueError(f"revision {revision_id} already exists with other content")
                return revision_id
            self._connection.execute(
                "INSERT INTO revisions (revision_id, created_at, payload_json) VALUES (?, ?, ?)",
                (revision_id, now, canonical_text),
            )
        return revision_id

    def revision(self, revision_id: str) -> Mapping[str, object]:
        """Fetch the payload of a revision by ID, returning an immutable mapping snapshot."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT payload_json FROM revisions WHERE revision_id = ?",
                (revision_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(f"unknown revision: {revision_id}")
            (payload_json,) = row
            return own_json_object(json.loads(payload_json))

    def set_session_overlay(self, session_id: str, owner: str, tier: str = "read-only") -> None:
        """Add (or keep) one owner's restrictive overlay on a session; tiers can only tighten."""
        if tier != "read-only":
            raise ValueError(f"unsupported overlay tier: {tier!r}")
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            read_session(self._connection, session_id)
            self._connection.execute(
                """
                INSERT INTO session_overlays (session_id, owner, tier, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (session_id, owner) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, owner, tier, now, now),
            )

    def clear_session_overlay(self, session_id: str, owner: str) -> bool:
        """Revoke exactly this owner's overlay; other owners and the core policy stay."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "DELETE FROM session_overlays WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            )
            return cursor.rowcount > 0

    def session_overlays(self, session_id: str) -> tuple[str, ...]:
        """Tiers currently restricting this session (one row per owner)."""
        with self._lock:
            self._check_open()
            rows = self._connection.execute(
                "SELECT tier FROM session_overlays WHERE session_id = ? ORDER BY owner ASC",
                (session_id,),
            ).fetchall()
            return tuple(tier for (tier,) in rows)

    def set_window_projection(
        self,
        session_id: str,
        owner: str,
        *,
        covered_from_sequence: int,
        covered_to_sequence: int,
        replacement: str,
        covered_bytes: int,
        dropped_bytes: int,
    ) -> None:
        """Record the session's verified window projection (one per session, latest wins)."""
        now = int(time.time() * 1000)
        with self._lock:
            self._check_open()
            read_session(self._connection, session_id)
            self._connection.execute(
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

    def window_projection(self, session_id: str) -> WindowProjectionRecord | None:
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                """
                SELECT session_id, owner, covered_from_sequence, covered_to_sequence,
                       replacement, covered_bytes, dropped_bytes, created_at
                FROM window_projections WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return _decode_projection(row)

    def commit_activation(
        self,
        *,
        workspace: str,
        session_id: str,
        expected_sequence: int,
        expected_run_id: str | None,
        expected_revision_id: str,
        target_revision_id: str,
        target_payload: Mapping[str, object],
        run_id: str,
        status: SessionStatus,
        events: Sequence[EventDraft],
        checkpoint_kind: CheckpointKind | None,
        checkpoint_payload: Mapping[str, object] | None,
        state_updates: Sequence[StagedStateUpdate] = (),
        cancelled_owners: Sequence[str] = (),
        cleared_budget_owners: Sequence[str] = (),
    ) -> SessionRecord:
        """One CAS transaction that switches the workspace to a new capability revision.

        Covers, atomically: the immutable revision row, every migrated/deactivated
        owner state copy, the workspace pointer, EVERY session ref in the workspace
        (including the acting running session), retargeted or cancelled queued work,
        cleared budgets, the acting session's events/checkpoint, and therefore the
        durable activation request consumption that those events record. Blockers
        are re-verified inside the transaction, so a racing writer cannot slip a
        second generation in.
        """
        target_text = canonical_json(target_payload)
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                now = int(time.time() * 1000)

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
                if session_row.workspace != workspace:
                    raise StoreConflict(f"session {session_id!r} belongs to workspace {session_row.workspace!r}")
                if session_row.last_event_sequence != expected_sequence:
                    raise SequenceConflict(
                        f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {session_row.last_event_sequence}"
                    )
                if expected_run_id is not None and session_row.run_id != expected_run_id:
                    raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {session_row.run_id!r}")
                if session_row.active_revision_id != expected_revision_id:
                    raise StoreConflict(
                        f"active revision mismatch for session {session_id!r}: "
                        f"active is {session_row.active_revision_id!r}, got {expected_revision_id!r}"
                    )
                if target_revision_id == expected_revision_id:
                    raise StoreConflict("the workspace already runs the target capability revision")

                blockers = self._activation_blockers_locked(workspace, session_id, exclude_claimed_in_session=True)
                if blockers:
                    raise StoreConflict(f"activation blocked: {'; '.join(blockers)}")

                self._connection.execute(
                    """
                    INSERT INTO revisions (revision_id, created_at, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (target_revision_id, now, target_text),
                )
                self._connection.execute(
                    """
                    INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (workspace) DO UPDATE SET
                        active_revision_id = excluded.active_revision_id,
                        updated_at = excluded.updated_at
                    """,
                    (workspace, target_revision_id, now),
                )
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET active_revision_id = ?, updated_at = ?
                    WHERE workspace = ?
                    """,
                    (target_revision_id, now, workspace),
                )

                apply_state_updates_locked(self._connection, state_updates, default_revision_id=target_revision_id, now=now)

                for owner in cancelled_owners:
                    # A deactivated owner's generic constraints die with it: an orphaned
                    # restrictive overlay must never keep blocking ordinary chat. Session-keyed
                    # rows are narrowed to this workspace's sessions; a shared database must
                    # never lose another workspace's overlays to this switch.
                    self._connection.execute(
                        """
                        DELETE FROM session_overlays
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    # A deactivated owner's projection disappears with it: the raw window
                    # becomes visible again instead of a summary nobody owns.
                    self._connection.execute(
                        """
                        DELETE FROM window_projections
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE background_actions
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    # A deactivated owner's pending wakeups die with it: a schedule nobody owns
                    # must never fire into the new generation. Claimed rows already have their
                    # run; settling them stays with that run's terminal path. Wakeups carry
                    # their own workspace column, so this narrows directly instead of joining
                    # sessions.
                    self._connection.execute(
                        """
                        UPDATE wakeups
                        SET status = 'cancelled', updated_at = ?
                        WHERE status = 'pending' AND owner = ? AND workspace = ?
                        """,
                        (now, owner, workspace),
                    )
                # Budgets die for cancelled owners AND for still-enabled owners the caller
                # explicitly clears (e.g. a budget reset across a generation that keeps the
                # owner): the union preserves that path, narrowed to this workspace.
                for owner in set(cancelled_owners) | set(cleared_budget_owners):
                    self._connection.execute(
                        """
                        DELETE FROM session_budgets
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                # Queued work that stays enabled keeps its provenance and shared root
                # budget, but is rebound to the revision that will execute it.
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET revision_id = ?, updated_at = ?
                    WHERE session_id = ? AND status IN ('pending', 'claimed')
                    """,
                    (target_revision_id, now, session_id),
                )
                self._connection.execute(
                    """
                    UPDATE background_actions
                    SET revision_id = ?, updated_at = ?
                    WHERE session_id = ? AND status IN ('pending', 'claimed')
                    """,
                    (target_revision_id, now, session_id),
                )

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
                    event_payload_json = canonical_json(draft.payload)
                    self._connection.execute(
                        """
                        INSERT INTO session_events (
                            session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            current_seq,
                            run_id,
                            draft.event_type,
                            draft.source,
                            draft.actor,
                            target_revision_id,
                            event_payload_json,
                            len(event_payload_json.encode("utf-8")),
                        ),
                    )
                    record_event_indexes(self._connection, session_id, draft.event_type, draft.payload, now)

                self._connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, run_id = ?, updated_at = ?, last_event_sequence = ?
                    WHERE session_id = ?
                    """,
                    (status.value, run_id, now, current_seq, session_id),
                )

                if checkpoint_kind is not None and checkpoint_payload is not None:
                    checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(checkpoint_payload)
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
                            target_revision_id,
                            run_id,
                            current_seq,
                            canonical_json(checkpoint_payload),
                            checkpoint_root_id,
                            checkpoint_approval,
                            checkpoint_ask_seq,
                            checkpoint_ask_req,
                            now,
                        ),
                    )

                self._connection.execute("COMMIT")
                in_transaction = False
                return SessionRecord(
                    session_id=session_id,
                    workspace=workspace,
                    status=status,
                    active_revision_id=target_revision_id,
                    run_id=run_id,
                    turn=session_row.turn,
                    last_event_sequence=current_seq,
                    root_session_id=session_row.root_session_id,
                    parent_session_id=session_row.parent_session_id,
                    depth=session_row.depth,
                    tool_allowlist=_decode_tool_allowlist(session_row.tool_allowlist_json),
                )
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def adopt_revision(
        self,
        *,
        workspace: str,
        session_id: str | None,
        expected_sequence: int | None,
        expected_run_id: str | None,
        expected_revision_id: str,
        target_revision_id: str,
        target_payload: Mapping[str, object],
        run_id: str,
        status: SessionStatus | None,
        events: Sequence[EventDraft] = (),
        checkpoint_kind: CheckpointKind | None = None,
        checkpoint_payload: Mapping[str, object] | None = None,
        state_updates: Sequence[StagedStateUpdate] = (),
        cancelled_owners: Sequence[str] = (),
        cleared_budget_owners: Sequence[str] = (),
        require_quiescent: bool,
        recover_orphaned_runs: bool = False,
        fact_observers: Mapping[ObservedFactKind, Sequence[str]] | None = None,
    ) -> str:
        """Atomic CAS transaction that commits a new revision and transitions the workspace."""
        target_text = canonical_json(target_payload)
        if session_id is None:
            if expected_sequence is not None or expected_run_id is not None:
                raise ValueError("adoption without an acting session must not carry session expectations")
        elif expected_sequence is None:
            raise ValueError("adoption with an acting session needs its expected sequence")
        if recover_orphaned_runs and fact_observers is None:
            raise ValueError("orphan recovery needs the target generation's fact observers")
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                now = int(time.time() * 1000)

                acting_was_running = False
                if session_id is None:
                    count = self._connection.execute("SELECT COUNT(*) FROM sessions WHERE workspace = ?", (workspace,)).fetchone()
                    if count is None:
                        raise StorageError("session count returned no row")
                    (session_count,) = count
                    if session_count != 0:
                        raise StoreConflict("adoption without an acting session requires a workspace with no sessions")
                else:
                    assert expected_sequence is not None
                    row = self._connection.execute(
                        """
                        SELECT workspace, status, active_revision_id, run_id, turn, last_event_sequence
                        FROM sessions
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    if row is None:
                        raise UnknownSession(f"session {session_id!r} does not exist", session_id)
                    adopt_row = _AdoptSessionColumns(*row)
                    adopt_status = _decode_closed_enum(SessionStatus, adopt_row.status, field="status", record=f"session {session_id!r}")
                    if adopt_row.workspace != workspace:
                        raise StoreConflict(f"session {session_id!r} belongs to workspace {adopt_row.workspace!r}")
                    if adopt_row.last_event_sequence != expected_sequence:
                        raise SequenceConflict(
                            f"sequence mismatch for session {session_id!r}: expected {expected_sequence}, got {adopt_row.last_event_sequence}"
                        )
                    if adopt_row.run_id != expected_run_id:
                        raise StoreConflict(f"run_id mismatch for session {session_id!r}: expected {expected_run_id!r}, got {adopt_row.run_id!r}")
                    if adopt_row.active_revision_id != expected_revision_id:
                        raise StoreConflict(
                            f"active revision mismatch for session {session_id!r}: active is {adopt_row.active_revision_id!r}, "
                            f"got {expected_revision_id!r}"
                        )
                    if target_revision_id == expected_revision_id:
                        raise StoreConflict("the workspace already runs the target capability revision")
                    if recover_orphaned_runs and adopt_status is SessionStatus.RUNNING:
                        # The caller proved no core owns the workspace, so this acting row is an
                        # orphan too. It may adopt only as interrupted, keeping its cursor for an
                        # explicit resume instead of ending the transaction still "running".
                        if status is not SessionStatus.INTERRUPTED or checkpoint_kind not in (None, CheckpointKind.INTERRUPTED):
                            raise StoreConflict("an orphaned acting session must be adopted as interrupted")
                        acting_was_running = True

                # The revision row precedes orphan settlement: the durable recovery fact it
                # appends carries the identity this transaction is about to publish.
                self._connection.execute(
                    """
                    INSERT INTO revisions (revision_id, created_at, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (target_revision_id, now, target_text),
                )
                if recover_orphaned_runs:
                    assert fact_observers is not None
                    self._settle_orphaned_runs_locked(
                        workspace,
                        target_revision_id=target_revision_id,
                        acting_session_id=session_id,
                        fact_observers=fact_observers,
                        now=now,
                    )
                blockers = self._adoption_blockers_locked(
                    workspace,
                    include_parked=require_quiescent,
                    # The acting orphan is settled by the caller's explicit interrupted commit;
                    # it is still subject to every non-running blocker below.
                    exclude_running=session_id if acting_was_running else None,
                )
                if blockers:
                    joined = "; ".join(f"{sess} is blocked ({reason})" for sess, reason in blockers)
                    raise StoreConflict(f"adoption blocked: {joined}")

                self._connection.execute(
                    """
                    INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (workspace) DO UPDATE SET
                        active_revision_id = excluded.active_revision_id,
                        updated_at = excluded.updated_at
                    """,
                    (workspace, target_revision_id, now),
                )
                self._connection.execute(
                    """
                    UPDATE sessions
                    SET active_revision_id = ?, updated_at = ?
                    WHERE workspace = ?
                    """,
                    (target_revision_id, now, workspace),
                )

                apply_state_updates_locked(self._connection, state_updates, default_revision_id=target_revision_id, now=now)

                for owner in cancelled_owners:
                    self._connection.execute(
                        """
                        DELETE FROM session_overlays
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    self._connection.execute(
                        """
                        DELETE FROM window_projections
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE continuations
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE background_actions
                        SET status = 'cancelled', updated_at = ?
                        WHERE status IN ('pending', 'claimed') AND owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (now, owner, workspace),
                    )
                    self._connection.execute(
                        """
                        UPDATE wakeups
                        SET status = 'cancelled', updated_at = ?
                        WHERE status = 'pending' AND owner = ? AND workspace = ?
                        """,
                        (now, owner, workspace),
                    )
                for owner in set(cancelled_owners) | set(cleared_budget_owners):
                    self._connection.execute(
                        """
                        DELETE FROM session_budgets
                        WHERE owner = ?
                          AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                        """,
                        (owner, workspace),
                    )
                # Queued work that stays enabled keeps its provenance and shared root budget, but is
                # rebound to the revision that will execute it - every session, not just the acting one.
                self._connection.execute(
                    """
                    UPDATE continuations
                    SET revision_id = ?, updated_at = ?
                    WHERE status IN ('pending', 'claimed')
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    """,
                    (target_revision_id, now, workspace),
                )
                self._connection.execute(
                    """
                    UPDATE background_actions
                    SET revision_id = ?, updated_at = ?
                    WHERE status IN ('pending', 'claimed')
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    """,
                    (target_revision_id, now, workspace),
                )
                # Every resumable cursor moves with the generation: paused and parked checkpoints
                # keep their payload and run identity, but a candidate that refused a stale
                # checkpoint revision would strand them. Terminal history is never read again.
                self._connection.execute(
                    """
                    UPDATE checkpoints
                    SET revision_id = ?, updated_at = ?
                    WHERE kind <> 'terminal'
                      AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                    """,
                    (target_revision_id, now, workspace),
                )

                if session_id is not None:
                    assert status is not None
                    current_seq = adopt_row.last_event_sequence
                    for draft in events:
                        current_seq += 1
                        event_payload_json = canonical_json(draft.payload)
                        self._connection.execute(
                            """
                            INSERT INTO session_events (
                                session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                session_id,
                                current_seq,
                                run_id,
                                draft.event_type,
                                draft.source,
                                draft.actor,
                                target_revision_id,
                                event_payload_json,
                                len(event_payload_json.encode("utf-8")),
                            ),
                        )
                        record_event_indexes(self._connection, session_id, draft.event_type, draft.payload, now)
                    self._connection.execute(
                        """
                        UPDATE sessions
                        SET status = ?, run_id = ?, updated_at = ?, last_event_sequence = ?
                        WHERE session_id = ?
                        """,
                        (status.value, run_id, now, current_seq, session_id),
                    )
                    if checkpoint_kind is not None and checkpoint_payload is not None:
                        checkpoint_root_id, checkpoint_approval, checkpoint_ask_seq, checkpoint_ask_req = _checkpoint_index_values(checkpoint_payload)
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
                                target_revision_id,
                                run_id,
                                current_seq,
                                canonical_json(checkpoint_payload),
                                checkpoint_root_id,
                                checkpoint_approval,
                                checkpoint_ask_seq,
                                checkpoint_ask_req,
                                now,
                            ),
                        )

                self._connection.execute("COMMIT")
                in_transaction = False
                return target_revision_id
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _adoption_blockers_locked(self, workspace: str, *, include_parked: bool, exclude_running: str | None = None) -> list[tuple[str, str]]:
        """Work that must settle before an adoption commits; callers hold the connection lock.

        ``exclude_running`` names the one acting session an orphan recovery is already settling
        explicitly; it still appears through every other blocker category.
        """
        blockers: list[tuple[str, str]] = []
        running_query = "SELECT session_id FROM sessions WHERE workspace = ? AND status = 'running'"
        running_params: list[object] = [workspace]
        if exclude_running is not None:
            running_query += " AND session_id <> ?"
            running_params.append(exclude_running)
        cursor = self._connection.execute(running_query + " ORDER BY session_id ASC", tuple(running_params))
        blockers.extend((session_id, "running") for (session_id,) in cursor.fetchall())
        if include_parked:
            cursor = self._connection.execute(
                """
                SELECT session_id
                FROM sessions
                WHERE workspace = ? AND status = 'waiting'
                ORDER BY session_id ASC
                """,
                (workspace,),
            )
            blockers.extend((session_id, "approval_wait") for (session_id,) in cursor.fetchall())
            cursor = self._connection.execute(
                """
                SELECT session_id, request_id
                FROM action_confirmations
                WHERE status = 'pending'
                  AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
                ORDER BY request_id ASC
                """,
                (workspace,),
            )
            blockers.extend((session_id, "pending_confirmation") for (session_id, _request_id) in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, continuation_id, owner
            FROM continuations
            WHERE status = 'claimed'
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY continuation_id ASC
            """,
            (workspace,),
        )
        blockers.extend((session_id, "claimed_continuation") for (session_id, _continuation_id, _owner) in cursor.fetchall())
        return blockers

    def _settle_orphaned_runs_locked(
        self,
        workspace: str,
        *,
        target_revision_id: str,
        acting_session_id: str | None,
        fact_observers: Mapping[ObservedFactKind, Sequence[str]],
        now: int,
    ) -> None:
        """Settle dead runs of ONE workspace inside the caller's adoption transaction.

        Only ``running`` rows of ``workspace`` are touched, and only because the caller proved no
        core owns it. Each becomes ``interrupted`` at its last committed boundary with one
        ``core.run_interrupted`` fact. The checkpoint payload (cursor, batch references, replay
        policies) is preserved byte-for-byte: native effects and unsettled dispatches stay exactly
        as unknown as they were, so an explicit resume applies the ordinary replay-policy and
        unknown-usage settlement instead of this transaction inventing a replay or a stop.
        """
        query = "SELECT session_id, run_id, last_event_sequence FROM sessions WHERE workspace = ? AND status = 'running'"
        params: list[object] = [workspace]
        if acting_session_id is not None:
            query += " AND session_id <> ?"
            params.append(acting_session_id)
        rows = self._connection.execute(query + " ORDER BY session_id ASC", tuple(params)).fetchall()
        eligible_owners = list(fact_observers.get("run_finished", ()))
        for session_id, run_id, last_sequence in rows:
            if not run_id:
                raise StoreConflict(f"running session {session_id!r} has no durable run identity")
            sequence = last_sequence + 1
            logical_turn = ""
            checkpoint = self._connection.execute(
                "SELECT payload_json FROM checkpoints WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if checkpoint is not None:
                (checkpoint_json,) = checkpoint
                payload = json.loads(checkpoint_json)
                if isinstance(payload, Mapping) and isinstance(payload.get("logical_turn"), str):
                    logical_turn = payload["logical_turn"]
            orphan_payload_json = canonical_json(
                {
                    "reason": "the process that owned this run is gone; adoption recovered the session at its last committed boundary",
                    "stop_cause": "shutdown",
                    "logical_turn": logical_turn,
                    "eligible_owners": eligible_owners,
                }
            )
            self._connection.execute(
                """
                INSERT INTO session_events (
                    session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes
                ) VALUES (?, ?, ?, ?, 'core', 'core', ?, ?, ?)
                """,
                (
                    str(session_id),
                    sequence,
                    str(run_id),
                    EventType.RUN_INTERRUPTED,
                    target_revision_id,
                    orphan_payload_json,
                    len(orphan_payload_json.encode("utf-8")),
                ),
            )
            self._connection.execute(
                "UPDATE sessions SET status = 'interrupted', last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                (sequence, now, str(session_id)),
            )
            # The cursor is kept as-is; only its status and generation move with the adoption.
            self._connection.execute(
                "UPDATE checkpoints SET kind = 'interrupted', revision_id = ?, last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                (target_revision_id, sequence, now, str(session_id)),
            )

    def workspace_current(self, workspace: str) -> str | None:
        """The capability revision the workspace is currently running, or None before first adoption."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                "SELECT active_revision_id FROM workspace_state WHERE workspace = ?",
                (workspace,),
            )
            row = cursor.fetchone()
            return None if row is None else row[0]

    def record_workspace_revision(self, workspace: str, revision_id: str) -> None:
        """Adopt a revision for a workspace without ever overwriting an existing pointer."""
        with self._lock:
            self._check_open()
            self._connection.execute(
                """
                INSERT INTO workspace_state (workspace, active_revision_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (workspace) DO NOTHING
                """,
                (workspace, revision_id, int(time.time() * 1000)),
            )

    def plugin_state_rows(self, scope: StateScope, scope_key: str) -> tuple[PluginStateRow, ...]:
        """Every owner state row in one scope, with its core-owned version metadata."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, scope, scope_key, payload_json, version, schema_version, revision_id
                FROM plugin_state
                WHERE scope = ? AND scope_key = ?
                ORDER BY owner ASC
                """,
                (scope, scope_key),
            )
            return tuple(_decode_plugin_state(row) for row in cursor.fetchall())

    def _activation_blockers_locked(self, workspace: str, acting_session_id: str, *, exclude_claimed_in_session: bool) -> list[str]:
        """Blockers that must stop a capability switch; callers hold the connection lock."""
        blockers: list[str] = []
        cursor = self._connection.execute(
            """
            SELECT session_id, status
            FROM sessions
            WHERE workspace = ? AND session_id <> ? AND status IN ('running', 'waiting')
            ORDER BY session_id ASC
            """,
            (workspace, acting_session_id),
        )
        blockers.extend(f"session {session_id} is {status}" for (session_id, status) in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, request_id
            FROM action_confirmations
            WHERE status = 'pending' AND session_id <> ?
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY request_id ASC
            """,
            (acting_session_id, workspace),
        )
        blockers.extend(f"session {session_id} awaits the decision for confirmation {request_id}" for (session_id, request_id) in cursor.fetchall())
        cursor = self._connection.execute(
            """
            SELECT session_id, continuation_id, owner, status
            FROM continuations
            WHERE status IN ('pending', 'claimed')
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY continuation_id ASC
            """,
            (workspace,),
        )
        for session_id, continuation_id, owner, status in cursor.fetchall():
            if session_id == acting_session_id:
                # The acting session may keep its own queued work; it is retargeted
                # by the activation transaction instead of being rejected.
                continue
            blockers.append(f"session {session_id} has a {status} continuation {continuation_id} owned by {owner}")
        cursor = self._connection.execute(
            """
            SELECT session_id, action_id, owner, status
            FROM background_actions
            WHERE status IN ('pending', 'claimed')
              AND session_id IN (SELECT session_id FROM sessions WHERE workspace = ?)
            ORDER BY action_id ASC
            """,
            (workspace,),
        )
        for session_id, action_id, owner, status in cursor.fetchall():
            if session_id == acting_session_id:
                continue
            blockers.append(f"session {session_id} has a {status} continuation {action_id} owned by {owner}")
        if exclude_claimed_in_session:
            cursor = self._connection.execute(
                """
                SELECT continuation_id, owner
                FROM continuations
                WHERE session_id = ? AND status = 'claimed'
                ORDER BY continuation_id ASC
                """,
                (acting_session_id,),
            )
            claimed = cursor.fetchall()
            if len(claimed) > 1:
                blockers.append(f"session {acting_session_id} has multiple claimed continuations")
        return blockers

    def activation_blockers(self, workspace: str, acting_session_id: str) -> tuple[str, ...]:
        """Read-only view of everything that must be idle before a capability switch."""
        with self._lock:
            self._check_open()
            return tuple(self._activation_blockers_locked(workspace, acting_session_id, exclude_claimed_in_session=False))

    def plugin_state(self, owner: str, scope: StateScope, scope_key: str) -> tuple[Mapping[str, object] | None, int]:
        """Fetch current plugin state and version for the given owner, scope, and scope_key."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT payload_json, version
                FROM plugin_state
                WHERE scope = ? AND scope_key = ? AND owner = ?
                """,
                (scope, scope_key, owner),
            )
            row = cursor.fetchone()
            if row is None:
                return (None, 0)
            (payload_json, version) = row
            return (own_json_object(json.loads(payload_json)), version)

    def confirmation(self, request_id: str) -> ActionConfirmationRecord | None:
        """Fetch an action confirmation record by request ID."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT request_id, session_id, owner, command_name, arguments_json, revision_id,
                       expected_state_version, ui_type, ui_data_json, status, created_at, decided_at
                FROM action_confirmations
                WHERE request_id = ?
                """,
                (request_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _decode_confirmation(row)

    def decide_confirmation(self, request_id: str, decision: ConfirmationStatus) -> None:
        """Transition a pending confirmation record to allowed, denied, or stale."""
        with self._lock:
            self._check_open()
            now = int(time.time() * 1000)
            cursor = self._connection.execute(
                """
                UPDATE action_confirmations
                SET status = ?, decided_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (decision.value, now, request_id),
            )
            if cursor.rowcount == 0:
                raise UnknownConfirmation(f"confirmation {request_id!r} is not in pending state or does not exist", request_id)

    def session_budgets(self, session_id: str) -> tuple[PluginBudget, ...]:
        """Fetch all active budget constraints for a session."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens, max_cost_usd, max_elapsed_seconds
                FROM session_budgets
                WHERE session_id = ?
                """,
                (session_id,),
            )
            return tuple(_decode_budget(row) for row in cursor.fetchall())

    def owner_budget(self, session_id: str, owner: str) -> PluginBudget | None:
        """Fetch active budget constraint for the bound owner in a session."""
        with self._lock:
            self._check_open()
            cursor = self._connection.execute(
                """
                SELECT owner, since_sequence, max_input_tokens, max_output_tokens, max_total_tokens, max_cost_usd, max_elapsed_seconds
                FROM session_budgets
                WHERE session_id = ? AND owner = ?
                """,
                (session_id, owner),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return _decode_budget(row)
