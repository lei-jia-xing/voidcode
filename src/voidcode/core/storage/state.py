"""Owner state and workspace projections storage domain.

Capability revisions, the workspace pointer, session overlays, verified window
projections, plugin state rows, action confirmations, and session/owner budgets.
Atomic activation/adoption transactions live in `activation` and `adoption`.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`. Public methods stay on the
facade via inheritance, so `store.<method>` addresses the same one store object.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from uuid import uuid4

from ...sdk.json_values import own_json_object
from ...sdk.plugin import StateScope
from ...sdk.services import PluginBudget
from ._database import _ConnectionStore
from ._rows import _decode_budget, _decode_confirmation, _decode_plugin_state, _decode_projection
from ._values import (
    ActionConfirmationRecord,
    ConfirmationStatus,
    PluginStateRow,
    UnknownConfirmation,
    WindowProjectionRecord,
    canonical_json,
)
from .writes import read_session


class _StateMixin(_ConnectionStore):
    """Revisions, workspace pointer, overlays/projections, plugin state, confirmations, budgets."""

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

    def session_overlay(self, session_id: str, owner: str) -> str | None:
        """One owner's overlay tier on this session, or None when it has no row."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                "SELECT tier FROM session_overlays WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            ).fetchone()
            return None if row is None else str(row[0])

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
