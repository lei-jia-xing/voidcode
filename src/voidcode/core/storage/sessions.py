"""Session/lineage storage domain.

Session rows, delegated-child creation, approval policies, session facts/meta, the
session list, and the `session_info` join. Event-stream reads live in `event_stream`;
atomic event/checkpoint commits live in `commits`.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`. Public methods stay on the
facade via inheritance, so `store.<method>` addresses the same one store object.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from uuid import uuid4

from ...sdk.json_values import own_json_object
from ...sdk.services import SessionInfo
from ..events import EventType
from ._database import _ConnectionStore
from ._rows import _decode_event, _decode_session, _decode_session_info
from ._values import (
    MAX_CHILD_LABEL_BYTES,
    EventDraft,
    FencedSubtree,
    SessionRecord,
    SessionStatus,
    StoreConflict,
    StoredEvent,
)
from .writes import insert_session_event_locked


class _SessionsMixin(_ConnectionStore):
    """Session rows, lineage, approval policies, session facts/meta, and the session list."""

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
                    insert_session_event_locked(
                        self._connection,
                        session_id,
                        1,
                        run_id="",
                        revision_id=revision_id,
                        draft=EventDraft(EventType.CHILD_RESERVED, "core", "plugin", birth_payload),
                        now=now,
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

    def approval_policies(self, session_id: str) -> list[dict[str, object]]:
        """Active approval policies granted in this session, folded from the event stream."""
        with self._lock:
            self._check_open()
            self.session(session_id)
            cursor = self._connection.execute(
                """
                SELECT event_type, payload_json, sequence
                FROM session_events
                WHERE session_id = ? AND event_type IN (?, ?)
                ORDER BY sequence ASC
                """,
                (session_id, EventType.APPROVAL_POLICY_GRANTED, EventType.APPROVAL_POLICY_REVOKED),
            )
            active: dict[str, dict[str, object]] = {}
            for event_type, payload_json, sequence in cursor.fetchall():
                try:
                    payload = json.loads(payload_json) if isinstance(payload_json, str) else {}
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                if event_type == EventType.APPROVAL_POLICY_GRANTED:
                    rule_id = payload.get("rule_id")
                    tool_name = payload.get("tool_name")
                    if isinstance(rule_id, str) and rule_id and isinstance(tool_name, str) and tool_name:
                        pattern = payload.get("pattern")
                        active[rule_id] = {
                            "rule_id": rule_id,
                            "tool_name": tool_name,
                            "pattern": str(pattern) if pattern is not None else None,
                            "created_at": int(payload.get("created_at", sequence)),
                        }
                elif event_type == EventType.APPROVAL_POLICY_REVOKED:
                    rule_id = payload.get("rule_id")
                    if isinstance(rule_id, str):
                        active.pop(rule_id, None)
            return list(active.values())

    def revoke_approval_policy(self, session_id: str, rule_id: str, *, revision_id: str) -> bool:
        """Revoke an active approval policy by committing an APPROVAL_POLICY_REVOKED event."""
        with self._lock:
            self._check_open()
            policies = self.approval_policies(session_id)
            if not any(p.get("rule_id") == rule_id for p in policies):
                return False
            session = self.session(session_id)
            self.commit_staged(
                session_id,
                expected_sequence=session.last_event_sequence,
                expected_run_id=session.run_id,
                run_id=session.run_id or uuid4().hex,
                revision_id=revision_id,
                events=(
                    EventDraft(
                        EventType.APPROVAL_POLICY_REVOKED,
                        "client",
                        "user",
                        {"rule_id": rule_id},
                    ),
                ),
                status=session.status,
            )
            return True

    def session_meta(self, session_id: str) -> tuple[str | None, int | None, int | None]:
        """Project summary (first user message), created_at, and updated_at for one session."""
        with self._lock:
            self._check_open()
            row = self._connection.execute(
                "SELECT created_at, updated_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None, None, None
            created_at, updated_at = int(row[0]), int(row[1])
            ev_row = self._connection.execute(
                """
                SELECT payload_json FROM session_events
                WHERE session_id = ? AND event_type = ?
                ORDER BY sequence ASC LIMIT 1
                """,
                (session_id, EventType.USER_MESSAGE),
            ).fetchone()
            summary: str | None = None
            if ev_row and isinstance(ev_row[0], str):
                try:
                    payload = json.loads(ev_row[0])
                    if isinstance(payload, dict):
                        content = payload.get("content")
                        if isinstance(content, str) and content.strip():
                            first_line = content.strip().splitlines()[0].strip()
                            summary = (first_line[:77] + "...") if len(first_line) > 80 else first_line
                except Exception:
                    pass
            return summary, created_at, updated_at

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
