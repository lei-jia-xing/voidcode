"""Queued user follow-ups: list, withdraw, claim, and run binding.

A follow-up is a client prompt queued behind the active run. It is a core-owned
continuation with `source='user_message'`; withdrawal and claim are CAS transitions that
never replay an already-started message.

This mixin is composed into `storage.store.SqliteStore`; it shares that store's one
connection and re-entrant lock through `_ConnectionStore`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from ..events import EventType
from ._database import _ConnectionStore
from ._values import (
    EventDraft,
    PendingFollowupRecord,
    StaleRevision,
    StoreConflict,
    UnknownContinuation,
)
from .writes import insert_session_event_locked, read_session


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


class _FollowupsMixin(_ConnectionStore):
    """Core-owned user follow-up queue at the run boundary."""

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
