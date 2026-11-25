"""Capability activation: one CAS transaction switching the workspace revision.

`commit_activation` covers the immutable revision row, migrated/deactivated owner state,
the workspace pointer, every session ref in the workspace, retargeted or cancelled queued
work, cleared budgets, the acting session's events/checkpoint, and the durable activation
request consumption those events record. Blockers are re-verified inside the transaction,
so a racing writer cannot slip a second generation in.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

from ..events import EventType
from ._database import _ConnectionStore
from ._rows import _checkpoint_index_values, _decode_tool_allowlist, _SessionColumns
from ._values import (
    CheckpointKind,
    EventDraft,
    SequenceConflict,
    SessionRecord,
    SessionStatus,
    StagedStateUpdate,
    StoreConflict,
    UnknownSession,
    _int_value,
    canonical_json,
)
from .writes import apply_state_updates_locked, record_event_indexes, set_window_projection_locked


class _ActivationMixin(_ConnectionStore):
    """Atomic capability activation and its read-only blocker view."""

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
