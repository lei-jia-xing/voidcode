"""Background action queue: enqueue, claim, settle, and idempotent lookup.

Background actions are driver-run work stored separately from model continuations, with
their own idempotency namespace and root-authorization columns. Claims and settles never
rewrite the owner's action payload; a driver error is recorded in its own column.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping

from ._database import _ConnectionStore
from ._rows import _BackgroundActionColumns, _decode_background_action, _stored_root_budget
from ._values import (
    MAX_CONTINUATION_PAYLOAD_BYTES,
    ContinuationRecord,
    ContinuationStatus,
    StoreConflict,
    canonical_json,
)


class _BackgroundActionsMixin(_ConnectionStore):
    """Durable background-action queue with claim identity and captured root authority."""

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
