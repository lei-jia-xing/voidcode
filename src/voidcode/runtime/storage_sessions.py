from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, cast

from .contracts import (
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionResult,
    UnknownSessionError,
)
from .events import (
    EventEnvelope,
    EventSource,
)
from .session import (
    SessionRef,
    SessionState,
    SessionStatus,
    StoredSessionSummary,
    normalize_persisted_session_metadata,
    session_metadata_for_persistence,
)
from .storage_shared import _assert_terminal_session_events_allowed

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _SessionStorageMixin(_MixinBase):
    def _write_session_snapshot(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        pending_approval_json: str | None,
        pending_question_json: str | None,
        resume_checkpoint: dict[str, object],
        seal_terminal_status: bool = True,
    ) -> int:
        """Persist session row metadata and todo state.

        Boundary contract (storage is append-only truth, context_window is the
        sole read-time projection layer):

        - The event log is NOT written here. Events are appended incrementally
          by the run loop via ``append_session_events``; this method only seals
          the terminal session-row snapshot (status, output, metadata, turn,
          prompt, updated_at, todos, resume checkpoint).
        - ``last_event_sequence`` is never regressed: it is set to the maximum
          of the row's existing value (maintained by incremental appends) and
          the highest sequence in ``response.events``.
        - Metadata is bounded for safety via ``session_metadata_for_persistence``
          (secret scrubbing, length limits) — that is a safety bound, not
          context compaction. Context projection lives in ``context_window.py``.
        - When ``seal_terminal_status`` is False the row is written as
          ``interrupted`` instead of the terminal status: a newer run on the
          same session is still active, so the terminal seal must not clobber
          it (the incremental event log means overlapping runs can coexist).
        """
        session_id = response.session.session.id
        events = response.events
        persisted_metadata = session_metadata_for_persistence(
            response.session.metadata,
            events=events,
        )
        created_at = self._read_created_at(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        created_at_unix_ms = self._read_created_at_unix_ms(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        if created_at_unix_ms is None:
            created_at_unix_ms = int(time() * 1000)
        updated_at = self._next_timestamp(connection=connection)
        # The row watermark (maintained by every incremental
        # ``append_session_events``) IS the persisted truth. Clamp the sealed
        # value to the actual ``session_events`` max so a response whose
        # trailing events were only locally sequenced (resume paths resequence
        # client-only events like MCP/hook/release chunks that are never
        # appended) can never inflate the watermark beyond the durable event
        # log — a phantom sequence would break replay, resume truncation, and
        # notification reference-integrity.
        last_event_sequence = max(
            self._read_last_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            ),
            self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            ),
        )
        status = response.session.status if seal_terminal_status else "interrupted"
        _ = connection.execute(
            """
            INSERT OR REPLACE INTO sessions (
                session_id, parent_session_id, workspace_id, status, turn, prompt, output,
                metadata_json, pending_approval_json, pending_question_json,
                resume_checkpoint_json, created_at, updated_at,
                last_event_sequence, created_at_unix_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                response.session.session.parent_id,
                str(workspace),
                status,
                response.session.turn,
                request.prompt,
                response.output,
                json.dumps(persisted_metadata, sort_keys=True),
                pending_approval_json,
                pending_question_json,
                json.dumps(resume_checkpoint, sort_keys=True),
                created_at,
                updated_at,
                last_event_sequence,
                created_at_unix_ms,
            ),
        )
        self._replace_session_todos(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
            metadata=persisted_metadata,
        )
        return updated_at

    @staticmethod
    def _checkpoint_skill_snapshot(
        metadata: dict[str, object],
    ) -> tuple[object | None, object | None, dict[str, object]]:
        snapshot_payload = metadata.get("skill_snapshot")
        snapshot = cast(dict[str, object], snapshot_payload) if isinstance(snapshot_payload, dict) else {}
        binding_payload = snapshot.get("binding_snapshot")
        binding_snapshot = cast(dict[str, object], binding_payload) if isinstance(binding_payload, dict) else {}
        return snapshot.get("snapshot_hash"), snapshot.get("snapshot_version"), binding_snapshot

    def save_run(
        self,
        *,
        workspace: Path,
        request: RuntimeRequest,
        response: RuntimeResponse,
        clear_pending_approval: bool = True,
        seal_terminal_status: bool = True,
    ) -> None:
        """Seal the terminal session-row state for a completed run.

        Boundary: this is a terminal seal-writer — it writes the ``sessions``
        row snapshot (status, output, metadata, turn, prompt, updated_at,
        todos) and the terminal ``resume_checkpoint_json``, but it does NOT
        write ``session_events`` rows. The event log is persisted incrementally
        by the run loop via ``append_session_events``; this method only seals
        the terminal state and never regresses ``last_event_sequence``.
        Context assembly lives in ``context_window.py``.

        ``seal_terminal_status=False`` writes the row as ``interrupted`` instead
        of the terminal status, so an older-finishing run cannot re-seal a
        session that a newer run is still actively appending to.
        """
        session_id = response.session.session.id
        with self._write_connect(workspace) as connection:
            # The durable event-log watermark — see ``_write_session_snapshot``.
            # The resume checkpoint and the terminal notification must reference
            # this persisted truth, never a locally-resequenced response tail.
            persisted_last_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            updated_at = self._write_session_snapshot(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval_json=(
                    None
                    if clear_pending_approval
                    else self._read_pending_approval_json(
                        connection=connection,
                        workspace=workspace,
                        session_id=session_id,
                    )
                ),
                pending_question_json=None,
                resume_checkpoint=self._run_resume_checkpoint(
                    request=request,
                    response=response,
                    last_event_sequence=persisted_last_sequence,
                ),
                seal_terminal_status=seal_terminal_status,
            )
            self._sync_background_task_durable_state(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
            )
            self._sync_notifications(
                connection=connection,
                workspace=workspace,
                request=request,
                response=response,
                pending_approval=None,
                notification_run_id=updated_at,
                last_event_sequence=persisted_last_sequence,
            )
            connection.commit()

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]:
        self._auto_prune_sessions_for_list(workspace=workspace)
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                SELECT session_id, parent_session_id, status, turn, prompt, updated_at
                FROM sessions
                WHERE workspace_id = ?
                ORDER BY updated_at DESC, session_id ASC
                """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(
            StoredSessionSummary(
                session=SessionRef(
                    id=cast(str, row["session_id"]),
                    parent_id=cast(str | None, row["parent_session_id"]),
                ),
                status=self._parse_session_status(cast(str, row["status"])),
                turn=cast(int, row["turn"]),
                prompt=cast(str, row["prompt"]),
                updated_at=cast(int, row["updated_at"]),
            )
            for row in rows
        )

    def _auto_prune_sessions_for_list(self, *, workspace: Path) -> None:
        with self._write_connect(workspace) as connection:
            self._auto_prune_sessions(connection=connection, workspace=workspace)
            connection.commit()

    def append_session_event(
        self,
        *,
        workspace: Path,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope | None:
        """Append a single event to the session_events table — append-only, never modifies.

        Boundary: no compaction, no merging, no truncation. Events are append-only
        truth. Context projection (what the model sees) is handled exclusively by
        ``context_window.py``. This method and ``append_session_events`` are the
        only writers of ``session_events`` rows.
        """
        with self._write_connect(workspace) as connection:
            payload = self._enriched_background_task_event_payload(
                connection=connection,
                workspace=workspace,
                event_type=event_type,
                payload=payload,
            )
            # Verify the session exists before mutating any session state. We hold a
            # write lock from BEGIN IMMEDIATE, so this read is consistent with the
            # subsequent UPDATE.
            existing_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT status
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if existing_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            # Same authoritative seal as the batch path: a sealed terminal
            # session rejects every late non-lifecycle event, no matter which
            # append entry point delivers it.
            _assert_terminal_session_events_allowed(
                session_id=session_id,
                status=cast(str, existing_row["status"]),
                events=((event_type, source, payload, dedupe_key),),
            )
            # Claim the dedupe slot before touching the session row. Losing the
            # race means this is a duplicate delivery, and duplicate deliveries
            # must not perturb session ordering or sequence counters.
            if dedupe_key is not None:
                delivered_at = self._next_auxiliary_timestamp(connection=connection)
                inserted_delivery = connection.execute(
                    """
                    INSERT OR IGNORE INTO session_event_deliveries (
                        workspace_id, session_id, dedupe_key, delivered_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (str(workspace), session_id, dedupe_key, delivered_at),
                )
                if inserted_delivery.rowcount == 0:
                    connection.commit()
                    return None
            updated_at = self._next_timestamp(connection=connection)
            sequence_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    UPDATE sessions
                    SET updated_at = ?, last_event_sequence = last_event_sequence + 1
                    WHERE workspace_id = ? AND session_id = ?
                    RETURNING last_event_sequence
                    """,
                    (updated_at, str(workspace), session_id),
                ).fetchone(),
            )
            if sequence_row is None:
                # Session disappeared mid-transaction; should not happen under
                # BEGIN IMMEDIATE but kept defensively.
                raise UnknownSessionError(f"unknown session: {session_id}")
            sequence = cast(int, sequence_row["last_event_sequence"])
            event = EventEnvelope(
                session_id=session_id,
                sequence=sequence,
                event_type=event_type,
                source=source,
                payload=payload,
            )
            _ = connection.execute(
                """
                INSERT INTO session_events (
                    workspace_id, session_id, sequence, event_type, source, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(workspace),
                    event.session_id,
                    event.sequence,
                    event.event_type,
                    event.source,
                    json.dumps(event.payload, sort_keys=True),
                ),
            )
            connection.commit()
            return event

    def append_session_events(
        self,
        *,
        workspace: Path,
        session_id: str,
        events: tuple[tuple[str, EventSource, dict[str, object], str | None], ...],
        interrupted_checkpoint: dict[str, object] | None = None,
    ) -> tuple[EventEnvelope, ...]:
        """Append a batch of session events in one transaction — append-only.

        Mirrors ``append_session_event`` per event (dedupe slot via
        ``session_event_deliveries``, DB-assigned sequence via the
        ``last_event_sequence`` bump) but holds a single ``BEGIN IMMEDIATE``
        transaction so the whole batch is atomic. Terminal sessions reject
        non-lifecycle events via ``SessionSealedError``; an optional interrupted
        checkpoint is upserted in the same transaction.
        """
        with self._write_connect(workspace) as connection:
            status_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT status FROM sessions WHERE workspace_id = ? AND session_id = ?",
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if status_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            status = cast(str, status_row["status"])
            _assert_terminal_session_events_allowed(
                session_id=session_id,
                status=status,
                events=events,
            )
            assigned: list[EventEnvelope] = []
            for event_type, source, payload, dedupe_key in events:
                payload = self._enriched_background_task_event_payload(
                    connection=connection,
                    workspace=workspace,
                    event_type=event_type,
                    payload=payload,
                )
                if dedupe_key is not None:
                    delivered_at = self._next_auxiliary_timestamp(connection=connection)
                    inserted_delivery = connection.execute(
                        """
                        INSERT OR IGNORE INTO session_event_deliveries (
                            workspace_id, session_id, dedupe_key, delivered_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (str(workspace), session_id, dedupe_key, delivered_at),
                    )
                    if inserted_delivery.rowcount == 0:
                        continue
                updated_at = self._next_timestamp(connection=connection)
                sequence_row = cast(
                    sqlite3.Row | None,
                    connection.execute(
                        """
                        UPDATE sessions
                        SET updated_at = ?, last_event_sequence = last_event_sequence + 1
                        WHERE workspace_id = ? AND session_id = ?
                        RETURNING last_event_sequence
                        """,
                        (updated_at, str(workspace), session_id),
                    ).fetchone(),
                )
                if sequence_row is None:
                    raise UnknownSessionError(f"unknown session: {session_id}")
                sequence = cast(int, sequence_row["last_event_sequence"])
                event = EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type=event_type,
                    source=source,
                    payload=payload,
                )
                _ = connection.execute(
                    """
                    INSERT INTO session_events (
                        workspace_id, session_id, sequence, event_type, source, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(workspace),
                        event.session_id,
                        event.sequence,
                        event.event_type,
                        event.source,
                        json.dumps(event.payload, sort_keys=True),
                    ),
                )
                assigned.append(event)
            if interrupted_checkpoint is not None:
                checkpoint_updated_at = self._next_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'interrupted', resume_checkpoint_json = ?, updated_at = ?
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (
                        json.dumps(interrupted_checkpoint, sort_keys=True),
                        checkpoint_updated_at,
                        str(workspace),
                        session_id,
                    ),
                )
            connection.commit()
            return tuple(assigned)

    def save_interrupted_checkpoint(
        self,
        *,
        workspace: Path,
        session_id: str,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...],
        last_event_sequence: int,
        output: str | None = None,
        create_if_missing: bool = True,
        turn: int = 1,
        parent_session_id: str | None = None,
    ) -> None:
        """Persist a lightweight ``interrupted`` resume checkpoint to the sessions row.

        This is a cheap checkpoint of an in-flight run for resume-after-interrupt.
        Unlike ``save_run`` it does NOT write the ``session_events`` table, does
        NOT replace ``session_todos``, and does NOT write ``output`` /
        ``pending_approval_json`` / ``pending_question_json`` (``output`` is only
        preserved, never overwritten with NULL on the update path). It writes
        exactly one ``sessions`` row — creating it on first call when
        ``create_if_missing`` is set (mandatory: ``append_session_events`` raises
        ``UnknownSessionError`` when the row is absent, so the row must exist
        before the first event append).

        ``parent_session_id`` is persisted on both the insert and update paths so
        a child session's first (un-sealed) row already carries its parent — the
        child must reference its parent even when the run ends before a terminal
        seal (``_write_session_snapshot`` is the only other writer of
        ``parent_session_id``, and it only runs at seal time).

        ``tool_results`` must be the serialized ``ToolResult`` form produced by
        ``_tool_results_from_events`` and accepted by
        ``tool_results_from_checkpoint`` in ``resume.py``: a tuple of dicts, each
        carrying the identity keys ``tool_name`` (str), ``status`` (``"ok"`` |
        ``"error"``), ``data`` (dict), ``content`` (str | None), ``error``
        (str | None), plus — only when errored — the optional ``error_kind``,
        ``error_summary``, ``error_details`` (dict) and ``retry_guidance`` (str).
        Callers hold the durable events at this boundary and may derive these
        via ``_tool_results_from_events``.
        """
        checkpoint = self._interrupted_resume_checkpoint(
            prompt=prompt,
            session_metadata=session_metadata,
            tool_results=tool_results,
            last_event_sequence=last_event_sequence,
            output=output,
        )
        persisted_metadata = session_metadata_for_persistence(session_metadata)
        checkpoint_json = json.dumps(checkpoint, sort_keys=True)
        metadata_json = json.dumps(persisted_metadata, sort_keys=True)
        with self._write_connect(workspace) as connection:
            existing = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT 1 FROM sessions WHERE workspace_id = ? AND session_id = ?",
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if existing is None:
                if not create_if_missing:
                    raise UnknownSessionError(f"unknown session: {session_id}")
                created_at = self._read_created_at(
                    connection=connection,
                    workspace=workspace,
                    session_id=session_id,
                )
                updated_at = self._next_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, parent_session_id, workspace_id, status, turn, prompt, output,
                        metadata_json, pending_approval_json, pending_question_json,
                        resume_checkpoint_json, created_at, updated_at,
                        last_event_sequence, created_at_unix_ms
                    ) VALUES (?, ?, ?, 'interrupted', ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        parent_session_id,
                        str(workspace),
                        turn,
                        prompt,
                        output,
                        metadata_json,
                        checkpoint_json,
                        created_at,
                        updated_at,
                        last_event_sequence,
                        int(time() * 1000),
                    ),
                )
            else:
                _ = connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'interrupted',
                        resume_checkpoint_json = ?,
                        metadata_json = ?,
                        prompt = ?,
                        output = COALESCE(?, output),
                        parent_session_id = COALESCE(?, parent_session_id),
                        updated_at = ?,
                        last_event_sequence = MAX(last_event_sequence, ?)
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (
                        checkpoint_json,
                        metadata_json,
                        prompt,
                        output,
                        parent_session_id,
                        self._next_timestamp(connection=connection),
                        last_event_sequence,
                        str(workspace),
                        session_id,
                    ),
                )
            connection.commit()

    def truncate_session_events_after(self, *, workspace: Path, session_id: str, sequence: int) -> None:
        """Delete orphaned-tail ``session_events`` rows past a sequence for resume.

        Scoped to a single (workspace, session) pair in one transaction. Rows with
        ``sequence > sequence`` are removed so a resumed run can re-append a clean
        tail after the checkpoint without leaving stale trailing events. Rows at
        or below ``sequence`` are untouched. The session's ``last_event_sequence``
        watermark is reset to the surviving max so subsequent appends continue
        contiguously instead of skipping the freed sequence range.
        """
        with self._write_connect(workspace) as connection:
            _ = connection.execute(
                "DELETE FROM session_events WHERE workspace_id = ? AND session_id = ? AND sequence > ?",
                (str(workspace), session_id, sequence),
            )
            _ = connection.execute(
                """
                UPDATE sessions
                SET last_event_sequence = (
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM session_events
                    WHERE workspace_id = ? AND session_id = ?
                )
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id, str(workspace), session_id),
            )
            connection.commit()

    @staticmethod
    def _tool_results_from_events(events: tuple[EventEnvelope, ...]) -> list[dict[str, object]]:
        tool_results: list[dict[str, object]] = []
        for event in events:
            if event.event_type != "runtime.tool_completed":
                continue
            payload = event.payload
            raw_status = payload.get("status")
            is_err = raw_status == "error"
            if raw_status not in {"ok", "error"}:
                is_err = payload.get("error") is not None
            raw_content = payload.get("content")
            raw_error = payload.get("error")
            tool_result: dict[str, object] = {
                "tool_name": str(payload.get("tool", "unknown")),
                "content": str(raw_content) if raw_content is not None and not is_err else None,
                "status": "error" if is_err else "ok",
                "data": payload,
                "error": str(raw_error) if raw_error is not None and is_err else None,
            }
            if is_err:
                if payload.get("error_kind") is not None:
                    tool_result["error_kind"] = str(payload.get("error_kind"))
                if payload.get("error_summary") is not None:
                    tool_result["error_summary"] = str(payload.get("error_summary"))
                if isinstance(payload.get("error_details"), dict):
                    tool_result["error_details"] = cast(dict[str, object], payload.get("error_details"))
                if payload.get("retry_guidance") is not None:
                    tool_result["retry_guidance"] = str(payload.get("retry_guidance"))
            tool_results.append(tool_result)
        return tool_results

    def has_session(self, *, workspace: Path, session_id: str) -> bool:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT 1
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        return row is not None

    def read_recent_tool_results(self, *, workspace: Path, session_id: str) -> str | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT recent_tool_results_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            return None
        return cast(str | None, row["recent_tool_results_json"])

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse:
        """Return ALL persisted events for a session, unfiltered except for revert markers.

        Boundary: storage returns every event — no compaction, no truncation, no
        context-window projection. The caller (or context_window.py) decides what
        subset to present to the model.
        """
        return self._load_session_response(
            workspace=workspace,
            session_id=session_id,
            filter_reverted=True,
        )

    def load_session_status(self, *, workspace: Path, session_id: str) -> SessionStatus:
        """Return the persisted row status for a session.

        Lightweight read used by the runtime's terminal-seal guard
        (``VoidCodeRuntime._sealed_session_status``): the guard must inspect the
        durable status without materializing the full event log.
        """
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT status FROM sessions WHERE workspace_id = ? AND session_id = ?",
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        return self._parse_session_status(cast(str, row["status"]))

    def update_session_metadata(self, *, workspace: Path, session_id: str, metadata: dict[str, object]) -> None:
        """Persist bounded runtime metadata without fabricating a new response."""
        persisted = session_metadata_for_persistence(metadata)
        with self._write_connect(workspace) as connection:
            updated = connection.execute(
                "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE workspace_id = ? AND session_id = ?",
                (json.dumps(persisted, sort_keys=True), self._next_timestamp(connection=connection), str(workspace), session_id),
            ).rowcount
            if updated != 1:
                raise UnknownSessionError(f"unknown session: {session_id}")
            connection.commit()

    def _load_session_response(
        self,
        *,
        workspace: Path,
        session_id: str,
        filter_reverted: bool,
    ) -> RuntimeResponse:
        """Load a session with ALL events from durable storage.

        Boundary: returns every stored event row unfiltered — no compaction,
        no truncation, no context-driven dropping. The only filter applied is
        the revert marker (when ``filter_reverted=True``), which is a user
        intent, not a storage-level compaction.
        """
        with self._connect(workspace) as connection:
            session_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                SELECT session_id, parent_session_id, status, turn, output, metadata_json
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
            if session_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            event_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                SELECT sequence, event_type, source, payload_json
                FROM session_events
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY sequence ASC
                """,
                    (str(workspace), session_id),
                ).fetchall(),
            )
            stored_todo_state = self._todo_state_from_rows(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
        metadata = self._metadata_with_todo_state(
            normalize_persisted_session_metadata(cast(dict[str, object], json.loads(cast(str, session_row["metadata_json"])))),
            stored_todo_state,
        )
        session = SessionState(
            session=SessionRef(
                id=cast(str, session_row["session_id"]),
                parent_id=cast(str | None, session_row["parent_session_id"]),
            ),
            status=self._parse_session_status(cast(str, session_row["status"])),
            turn=cast(int, session_row["turn"]),
            metadata=metadata,
        )
        events = tuple(
            EventEnvelope(
                session_id=session_id,
                sequence=cast(int, row["sequence"]),
                event_type=cast(str, row["event_type"]),
                source=self._parse_event_source(cast(str, row["source"])),
                payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
            )
            for row in event_rows
        )
        marker = self._revert_marker_from_metadata(session.metadata)
        output = cast(str | None, session_row["output"])
        if filter_reverted and marker is not None and marker.active:
            events = tuple(event for event in events if event.sequence < marker.sequence)
            session = SessionState(
                session=session.session,
                status=session.status,
                turn=session.turn,
                metadata=self._active_revert_metadata(session.metadata, events=events),
            )
            output = None
        return RuntimeResponse(session=session, events=events, output=output)

    def load_session_result(self, *, workspace: Path, session_id: str) -> RuntimeSessionResult:
        response = self._load_session_response(
            workspace=workspace,
            session_id=session_id,
            filter_reverted=False,
        )
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT prompt
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id),
                ).fetchone(),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        prompt = cast(str, row["prompt"])
        summary, error = self._result_summary(response=response, prompt=prompt)
        return RuntimeSessionResult(
            session=response.session,
            prompt=prompt,
            status=response.session.status,
            summary=summary,
            output=response.output,
            error=error,
            transcript=response.events,
            last_event_sequence=response.events[-1].sequence if response.events else 0,
            revert_marker=self._revert_marker_from_metadata(response.session.metadata),
        )

    @staticmethod
    def _result_summary(*, response: RuntimeResponse, prompt: str) -> tuple[str, str | None]:
        if response.session.status == "completed":
            output = (response.output or "").strip()
            if output:
                return f"Completed: {output[:120]}", None
            return f"Completed session for prompt: {prompt[:80]}", None
        if response.session.status == "waiting":
            for event in reversed(response.events):
                if event.event_type == "runtime.approval_requested":
                    tool = str(event.payload.get("tool", "tool"))
                    target = str(event.payload.get("target_summary", "")).strip()
                    if target:
                        return f"Approval blocked on {tool}: {target[:100]}", None
                    return f"Approval blocked on {tool}", None
                if event.event_type == "runtime.question_requested":
                    question_count = event.payload.get("question_count")
                    if isinstance(question_count, int) and question_count > 0:
                        label = "question" if question_count == 1 else "questions"
                        return f"Question blocked on {question_count} {label}", None
                    return "Question blocked", None
            return "Approval blocked", None
        if response.session.status == "failed":
            for event in reversed(response.events):
                if event.event_type == "runtime.failed":
                    error = str(event.payload.get("error", "runtime failed"))
                    return f"Failed: {error[:120]}", error
            return "Failed", None
        return f"{response.session.status.capitalize()} session", None
