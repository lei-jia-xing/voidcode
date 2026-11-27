"""Workspace adoption, orphan settlement, and claimed-wakeup run recovery.

Adoption commits a new revision and transitions the workspace atomically, preserving
resumable cursors and settling dead runs of one workspace only when the caller proved no
core owns it. Claimed-wakeup recovery runs at startup under the workspace lease and
settles sessions whose claimed run died with its process.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence

from ...sdk.hooks import ObservedFactKind
from ..events import EventType
from ._database import _ConnectionStore
from ._rows import _AdoptSessionColumns, _checkpoint_index_values, _decode_closed_enum
from ._values import (
    CheckpointKind,
    EventDraft,
    SequenceConflict,
    SessionStatus,
    StagedStateUpdate,
    StorageError,
    StoreConflict,
    UnknownSession,
    canonical_json,
)
from .writes import (
    apply_state_updates_locked,
    event_stop_cause_locked,
    insert_session_event_locked,
    interrupted_terminal_status,
)


class _AdoptionMixin(_ConnectionStore):
    """Atomic revision adoption plus startup recovery of orphaned runs."""

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
                        insert_session_event_locked(
                            self._connection,
                            session_id,
                            current_seq,
                            run_id=run_id,
                            revision_id=target_revision_id,
                            draft=draft,
                            now=now,
                        )
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
                try:
                    payload = json.loads(checkpoint_json)
                except TypeError, ValueError:
                    payload = None
                if isinstance(payload, Mapping) and isinstance(payload.get("logical_turn"), str):
                    logical_turn = payload["logical_turn"]
            insert_session_event_locked(
                self._connection,
                str(session_id),
                sequence,
                run_id=str(run_id),
                revision_id=target_revision_id,
                draft=EventDraft(
                    EventType.RUN_INTERRUPTED,
                    "core",
                    "core",
                    {
                        "reason": "the process that owned this run is gone; adoption recovered the session at its last committed boundary",
                        "stop_cause": "shutdown",
                        "logical_turn": logical_turn,
                        "eligible_owners": eligible_owners,
                    },
                ),
                now=now,
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

    def recover_claimed_wakeup_runs(
        self,
        workspace: str,
        *,
        revision_id: str,
        fact_observers: Mapping[ObservedFactKind, Sequence[str]],
    ) -> tuple[tuple[str, str], ...]:
        """Recover sessions whose claimed wakeup run died with its process.

        Normal startup holds the workspace lease, so no live core owns a ``running``
        row found here. Each such session gets exactly one durable ``run_interrupted``
        at its last committed boundary (its checkpoint cursor, including any native
        batch references, is preserved), then its claimed wakeup settles. A parked
        ``waiting`` session is a legitimate resumable state and keeps its claim. A
        session that already reached a run-finished event is never replayed: only
        the stale claimed wakeup row is settled from the recorded outcome.
        """
        settled_status: dict[SessionStatus, str] = {
            SessionStatus.COMPLETED: "completed",
            SessionStatus.FAILED: "failed",
            SessionStatus.INTERRUPTED: "cancelled",
            SessionStatus.IDLE: "failed",
        }
        eligible_owners = list(fact_observers.get("run_finished", ()))
        recovered: list[tuple[str, str]] = []
        with self._lock:
            self._check_open()
            in_transaction = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                in_transaction = True
                now = int(time.time() * 1000)
                session_ids = self._connection.execute(
                    """
                    SELECT DISTINCT w.session_id
                    FROM wakeups w
                    WHERE w.workspace = ? AND w.status = 'claimed'
                    ORDER BY w.session_id ASC
                    """,
                    (workspace,),
                ).fetchall()
                for (session_id,) in session_ids:
                    row = self._connection.execute(
                        "SELECT status, run_id, last_event_sequence, active_revision_id FROM sessions WHERE session_id = ?",
                        (str(session_id),),
                    ).fetchone()
                    if row is None:
                        raise StoreConflict(f"claimed wakeup names unknown session {session_id!r}")
                    status = _decode_closed_enum(SessionStatus, str(row[0]), field="status", record=f"session {session_id!r}")
                    run_id = str(row[1]) if row[1] is not None else None
                    last_sequence = int(row[2])
                    session_revision = str(row[3])
                    if session_revision != revision_id:
                        raise StoreConflict(f"claimed wakeup run of session {session_id!r} belongs to another capability revision")
                    if status is SessionStatus.WAITING:
                        # An approval/ask park is not dead work: preserve the claim for resume.
                        continue
                    if status is SessionStatus.RUNNING:
                        if not run_id:
                            raise StoreConflict(f"running session {session_id!r} has no durable run identity")
                        latest = self._connection.execute(
                            "SELECT event_type, run_id FROM session_events WHERE session_id = ? AND sequence = ?",
                            (str(session_id), last_sequence),
                        ).fetchone()
                        if (
                            latest is not None
                            and latest[0]
                            in (
                                EventType.RUN_COMPLETED,
                                EventType.RUN_FAILED,
                                EventType.RUN_INTERRUPTED,
                            )
                            and latest[1] == run_id
                        ):
                            # The run already recorded its own finish; only the session row
                            # was not advanced (no replay, no second run-finished fact).
                            recorded = str(latest[0])
                            checkpoint_kind: CheckpointKind
                            if recorded == EventType.RUN_COMPLETED:
                                terminal_status = "completed"
                                new_session_status = SessionStatus.COMPLETED
                                checkpoint_kind = CheckpointKind.TERMINAL
                            elif recorded == EventType.RUN_FAILED:
                                terminal_status = "failed"
                                new_session_status = SessionStatus.FAILED
                                checkpoint_kind = CheckpointKind.TERMINAL
                            else:
                                stop_cause = event_stop_cause_locked(self._connection, str(session_id), last_sequence)
                                terminal_status = interrupted_terminal_status(stop_cause)
                                new_session_status = SessionStatus.INTERRUPTED
                                checkpoint_kind = CheckpointKind.TERMINAL if terminal_status == "cancelled" else CheckpointKind.INTERRUPTED
                            self._connection.execute(
                                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
                                (new_session_status.value, now, str(session_id)),
                            )
                            self._connection.execute(
                                "UPDATE checkpoints SET kind = ?, updated_at = ? WHERE session_id = ?",
                                (checkpoint_kind.value, now, str(session_id)),
                            )
                            self._connection.execute(
                                "UPDATE wakeups SET status = ?, updated_at = ? WHERE session_id = ? AND status = 'claimed'",
                                (terminal_status, now, str(session_id)),
                            )
                            recovered.append((str(session_id), terminal_status))
                            continue
                        logical_turn = ""
                        checkpoint = self._connection.execute(
                            "SELECT payload_json FROM checkpoints WHERE session_id = ?",
                            (str(session_id),),
                        ).fetchone()
                        if checkpoint is not None:
                            try:
                                payload = json.loads(checkpoint[0])
                            except TypeError, ValueError:
                                payload = None
                            if isinstance(payload, Mapping) and isinstance(payload.get("logical_turn"), str):
                                logical_turn = payload["logical_turn"]
                        sequence = last_sequence + 1
                        insert_session_event_locked(
                            self._connection,
                            str(session_id),
                            sequence,
                            run_id=run_id,
                            revision_id=session_revision,
                            draft=EventDraft(
                                EventType.RUN_INTERRUPTED,
                                "core",
                                "core",
                                {
                                    "reason": (
                                        "the process that owned this claimed wakeup run is gone; "
                                        "startup recovered the session at its last committed boundary"
                                    ),
                                    "stop_cause": "shutdown",
                                    "logical_turn": logical_turn,
                                    "eligible_owners": eligible_owners,
                                },
                            ),
                            now=now,
                        )
                        self._connection.execute(
                            "UPDATE sessions SET status = 'interrupted', last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                            (sequence, now, str(session_id)),
                        )
                        self._connection.execute(
                            "UPDATE checkpoints SET kind = 'interrupted', revision_id = ?,"
                            " last_event_sequence = ?, updated_at = ? WHERE session_id = ?",
                            (session_revision, sequence, now, str(session_id)),
                        )
                        self._connection.execute(
                            "UPDATE wakeups SET status = 'failed', updated_at = ? WHERE session_id = ? AND status = 'claimed'",
                            (now, str(session_id)),
                        )
                        recovered.append((str(session_id), "failed"))
                        continue
                    terminal_status = settled_status.get(status, "failed")
                    if status is SessionStatus.INTERRUPTED:
                        stopped = self._connection.execute(
                            """
                            SELECT payload_json FROM session_events
                            WHERE session_id = ? AND event_type = ? AND run_id = ?
                            ORDER BY sequence DESC LIMIT 1
                            """,
                            (str(session_id), EventType.RUN_INTERRUPTED, run_id),
                        ).fetchone()
                        if stopped is not None:
                            try:
                                payload = json.loads(stopped[0])
                            except TypeError, ValueError:
                                payload = None
                            if isinstance(payload, Mapping) and payload.get("stop_cause") in ("user_stop", "owner_cancel"):
                                terminal_status = "cancelled"
                            else:
                                terminal_status = "failed"
                    self._connection.execute(
                        "UPDATE wakeups SET status = ?, updated_at = ? WHERE session_id = ? AND status = 'claimed'",
                        (terminal_status, now, str(session_id)),
                    )
                    recovered.append((str(session_id), terminal_status))
                self._connection.execute("COMMIT")
                in_transaction = False
                return tuple(recovered)
            except Exception:
                if in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
