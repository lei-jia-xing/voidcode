from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any, cast

from ..agent_capability import validate_agent_capability_snapshot
from ..composition import CompositionRef, FrozenComposition, SessionCompositionOwner, TaskCompositionOwner
from ..contracts import (
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionCheckoutBoundaryError,
    RuntimeSessionResult,
    SessionTreePathError,
    UnknownSessionError,
)
from ..events import (
    RUNTIME_TODO_UPDATED,
    EventEnvelope,
    EventSource,
)
from ..execution.report_codec import parse_report_payload, report_payload
from ..interaction_queue import QueuedMessageKind, QueuedRuntimeMessage, drain_runtime_messages, enqueue_runtime_message
from ..session import (
    SessionEntrySummary,
    SessionRef,
    SessionState,
    SessionStatus,
    StoredSessionSummary,
    normalize_persisted_session_metadata,
    session_entry_preview,
    session_metadata_for_persistence,
)
from ..todos import runtime_todo_phases_from_payload, todo_state_payload
from .fork import _NON_TRANSFERABLE_METADATA_KEYS, _dangling_interaction
from .rows import (
    SessionCreatedAtRow,
    SessionCreatedAtUnixMsRow,
    SessionEventPageRow,
    SessionEventPageStateRow,
    SessionEventRow,
    SessionForkProvenanceRow,
    SessionLastEventSequenceRow,
    SessionLeafSequenceRow,
    SessionListRow,
    SessionLoadRow,
    SessionMetadataRow,
    SessionPromptTitleRow,
    SessionStatusMetadataRow,
    SessionStatusRow,
    SessionTitleRow,
    SessionTreeEventRow,
    decode_row,
    fetch_row,
    fetch_rows,
)
from .shared import _assert_terminal_session_events_allowed

if TYPE_CHECKING:
    from .shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


@dataclass(frozen=True, slots=True)
class SessionEventsAfter:
    """Flat durable event tail plus the row state a follow client needs.

    ``status`` is the persisted row status and ``metadata`` the raw persisted
    session metadata at read time; ``events`` are all stored rows with
    ``sequence > after_sequence`` in ascending order, not an active-path page.
    """

    status: SessionStatus
    metadata: dict[str, object]
    events: tuple[EventEnvelope, ...]


@dataclass(frozen=True, slots=True)
class SessionTreeEvent:
    """One stored event paired with the sequence it follows (its ancestor edge).

    ``parent_sequence`` is ``None`` for the session's first entry.
    """

    event: EventEnvelope
    parent_sequence: int | None


@dataclass(frozen=True, slots=True)
class SessionEventPage:
    """One immutable path page pinned to a stored leaf.

    ``max_sequence`` is the durable row watermark, which can exceed the leaf
    when abandoned events remain stored. ``next_after_sequence`` is set only
    when another page remains on this pinned path.
    """

    leaf_sequence: int | None
    max_sequence: int
    entries: tuple[SessionTreeEvent, ...]
    next_after_sequence: int | None


def session_event_path(
    entries: Sequence[SessionTreeEvent],
    *,
    target_sequence: int | None = None,
    leaf_sequence: int | None = None,
) -> tuple[EventEnvelope, ...]:
    """Return the root→leaf event chain ending at the target, oldest first.

    Walks ``parent_sequence`` backwards from ``target_sequence`` and reverses.
    With no ``target_sequence`` the session's current ``leaf_sequence`` is the
    target; an unset leaf (a session with no events) has no path and refuses.

    A ``parent_sequence`` naming an event that is not in ``entries``, or a
    cycle among the walked ancestors, means the stored tree lost its path: the
    walk refuses with :class:`SessionTreePathError` rather than truncating
    silently or looping forever. Only the path actually walked is checked — a
    cycle on an unrelated branch is not discovered here.
    """
    if target_sequence is None:
        if leaf_sequence is None:
            raise SessionTreePathError("session has no leaf: refusing to resolve an empty event path")
        target_sequence = leaf_sequence
    by_sequence = {entry.event.sequence: entry for entry in entries}
    chain: list[EventEnvelope] = []
    seen: set[int] = set()
    cursor: int | None = target_sequence
    while cursor is not None:
        if cursor in seen:
            raise SessionTreePathError(f"event ancestry contains a cycle at sequence {cursor}")
        seen.add(cursor)
        entry = by_sequence.get(cursor)
        if entry is None:
            raise SessionTreePathError(f"event ancestry is broken: sequence {cursor} is missing")
        chain.append(entry.event)
        cursor = entry.parent_sequence
    chain.reverse()
    return tuple(chain)


def _checked_out_prompt(path: tuple[EventEnvelope, ...]) -> str:
    """The prompt belonging to a checked-out position, for its resume checkpoint.

    The checkpoint records the prompt the resume re-runs, and after a checkout
    that is the last user request on the new path — the entry the user just
    resumed from. A position before any request (an event from a run that
    recorded none) has no prompt to name; the entry list renders as empty, and
    the row can still be continued with an explicit new-turn run.
    """
    for event in reversed(path):
        if event.event_type == "runtime.request_received":
            prompt = event.payload.get("prompt")
            if isinstance(prompt, str):
                return prompt
    return ""


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
          context compaction. Context projection lives in ``context/window.py``.
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
        persisted_metadata = self._merge_runtime_owned_metadata(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
            metadata=persisted_metadata,
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
        # INSERT OR REPLACE rewrites the whole row, so the user-set title (a row
        # column the run snapshot does not own) has to be carried forward
        # explicitly or every subsequent run would clear it.
        title = self._read_title(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        # Same reason as ``title``: provenance is a row column the run snapshot
        # does not own, so an upsert rewrite of a forked session must carry it
        # forward or the fork would lose its lineage on its first run.
        forked_from_session_id, forked_at_sequence = self._read_fork_provenance(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        # The tree position is owned by the incremental append path, not by this
        # snapshot: ``INSERT OR REPLACE`` rewrites every column, so an upsert
        # that did not carry it would reset the position on every seal.
        leaf_sequence = self._read_leaf_sequence(
            connection=connection,
            workspace=workspace,
            session_id=session_id,
        )
        updated_at = self._next_timestamp(connection=connection)
        # The row watermark (maintained by every incremental
        # ``append_session_events``) IS the persisted truth. Clamp the sealed
        # value to the actual ``session_events`` max so a response whose
        # trailing events were only locally sequenced (resume paths resequence
        # client-only events like MCP/hook/release chunks that are never
        # appended) can never inflate the watermark beyond the durable event
        # log — a phantom sequence would break replay and resume truncation.
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
                session_id, parent_session_id, workspace_id, status, turn, prompt, title, output,
                metadata_json, pending_approval_json, pending_question_json,
                resume_checkpoint_json, created_at, updated_at,
                last_event_sequence, leaf_sequence, created_at_unix_ms,
                forked_from_session_id, forked_at_sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                response.session.session.parent_id,
                str(workspace),
                status,
                response.session.turn,
                request.prompt,
                title,
                response.output,
                json.dumps(persisted_metadata, sort_keys=True),
                pending_approval_json,
                pending_question_json,
                json.dumps(resume_checkpoint, sort_keys=True),
                created_at,
                updated_at,
                last_event_sequence,
                leaf_sequence,
                created_at_unix_ms,
                forked_from_session_id,
                forked_at_sequence,
            ),
        )
        return updated_at

    @staticmethod
    def _todo_state_from_events(events: tuple[EventEnvelope, ...]) -> Mapping[str, object] | None:
        """Latest ``runtime.todo_updated`` on the given events.

        The caller must pass the session's root→leaf *path* (see
        :func:`session_event_path`), never the flat log: under a tree the flat
        log also holds abandoned branches, so scanning it would restore todos
        from a checked-out-away branch.
        """
        for event in reversed(events):
            if event.event_type != RUNTIME_TODO_UPDATED:
                continue
            phases = runtime_todo_phases_from_payload(event.payload.get("phases"))
            revision = event.payload.get("revision")
            if not isinstance(revision, int) or revision < 0:
                raise ValueError("runtime todo revision must be a non-negative integer")
            return todo_state_payload(phases, revision=revision)
        return None

    @staticmethod
    def _session_composition_ref(metadata: Mapping[str, object]) -> CompositionRef:
        ref = CompositionRef.model_validate(metadata.get("composition_ref"))
        snapshot = metadata.get("agent_capability_snapshot")
        if isinstance(snapshot, dict):
            validated = validate_agent_capability_snapshot(snapshot)
            if CompositionRef.model_validate(validated["composition_ref"]) != ref:
                raise ValueError("capability snapshot does not match its canonical composition reference")
        return ref

    @staticmethod
    def _merge_runtime_owned_metadata(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
        metadata: dict[str, object],
        initial: bool = False,
    ) -> dict[str, object]:
        """Preserve the queue owner's current state, including consumed absence.

        Snapshot writers own all other metadata; only explicit enqueue/drain
        transactions own pending input and its delivery cursor. Reading those
        fields within the same write transaction prevents stale snapshots from
        erasing newly queued input or resurrecting already consumed messages.
        """
        row = fetch_row(
            connection,
            "SELECT metadata_json FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is None:
            if not initial:
                raise ValueError("session must be created by its atomic canonical checkpoint writer")
            return metadata
        stored_metadata = json.loads(decode_row(row, SessionMetadataRow)["metadata_json"])
        if not isinstance(stored_metadata, dict):
            raise ValueError("session metadata must decode to an object")

        stored_ref = _SessionStorageMixin._session_composition_ref(stored_metadata)
        if _SessionStorageMixin._session_composition_ref(metadata) != stored_ref:
            raise ValueError("session composition reference is immutable")
        merged = dict(metadata)
        for key in ("pending_messages", "runtime_interaction_delivery_cursor"):
            if key in stored_metadata:
                merged[key] = stored_metadata[key]
            else:
                merged.pop(key, None)
        if "execution_composition" in stored_metadata:
            if "execution_composition" in metadata and metadata["execution_composition"] != stored_metadata["execution_composition"]:
                raise ValueError("session execution composition is immutable")
            merged["execution_composition"] = stored_metadata["execution_composition"]
        elif "execution_composition" in metadata:
            raise ValueError("only the initial canonical owner writer may publish a composition body")
        return merged

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
        Context assembly lives in ``context/window.py``.

        ``seal_terminal_status=False`` writes the row as ``interrupted`` instead
        of the terminal status, so an older-finishing run cannot re-seal a
        session that a newer run is still actively appending to.
        """
        session_id = response.session.session.id
        with self._write_connect(workspace) as connection:
            # The durable event-log watermark — see ``_write_session_snapshot``.
            # The resume checkpoint must reference this persisted truth, never a
            # locally-resequenced response tail.
            persisted_last_sequence = self._max_persisted_event_sequence(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            self._write_session_snapshot(
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
            connection.commit()

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummary, ...]:
        self._auto_prune_sessions_for_list(workspace=workspace)
        with self._connect(workspace) as connection:
            rows = fetch_rows(
                connection,
                """
                SELECT session_id, parent_session_id, status, turn, prompt, title, forked_from_session_id,
                       forked_at_sequence, updated_at
                FROM sessions
                WHERE workspace_id = ?
                ORDER BY updated_at DESC, session_id ASC
                """,
                (str(workspace),),
            )
        decoded_rows = tuple(decode_row(row, SessionListRow) for row in rows)
        return tuple(
            StoredSessionSummary(
                session=SessionRef(
                    id=row["session_id"],
                    parent_id=row["parent_session_id"],
                ),
                status=self._parse_session_status(row["status"]),
                turn=row["turn"],
                prompt=row["prompt"],
                updated_at=row["updated_at"],
                title=row["title"],
                forked_from_session_id=row["forked_from_session_id"],
                forked_at_sequence=row["forked_at_sequence"],
            )
            for row in decoded_rows
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
        ``context/window.py``. This method and ``append_session_events`` are the
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
            existing_row = fetch_row(
                connection,
                """
                SELECT status
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            )
            if existing_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            # Same authoritative seal as the batch path: a sealed terminal
            # session rejects every late non-lifecycle event, no matter which
            # append entry point delivers it.
            _assert_terminal_session_events_allowed(
                session_id=session_id,
                status=decode_row(existing_row, SessionStatusRow)["status"],
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
                        workspace_id, session_id, dedupe_key, delivered_at, event_sequence
                    ) SELECT ?, ?, ?, ?, last_event_sequence + 1 FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (str(workspace), session_id, dedupe_key, delivered_at, str(workspace), session_id),
                )
                if inserted_delivery.rowcount == 0:
                    connection.commit()
                    return None
            # The tree position this append hangs off: the session's current
            # leaf. Read before the bump because the same UPDATE advances it.
            leaf_row = fetch_row(
                connection,
                "SELECT leaf_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            )
            parent_sequence = None if leaf_row is None else decode_row(leaf_row, SessionLeafSequenceRow)["leaf_sequence"]
            updated_at = self._next_timestamp(connection=connection)
            sequence_row = fetch_row(
                connection,
                """
                    UPDATE sessions
                    SET updated_at = ?, last_event_sequence = last_event_sequence + 1,
                        leaf_sequence = last_event_sequence + 1
                    WHERE workspace_id = ? AND session_id = ?
                    RETURNING last_event_sequence
                    """,
                (updated_at, str(workspace), session_id),
            )
            if sequence_row is None:
                # Session disappeared mid-transaction; should not happen under
                # BEGIN IMMEDIATE but kept defensively.
                raise UnknownSessionError(f"unknown session: {session_id}")
            sequence = decode_row(sequence_row, SessionLastEventSequenceRow)["last_event_sequence"]
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
                    workspace_id, session_id, sequence, parent_sequence, event_type, source, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(workspace),
                    event.session_id,
                    event.sequence,
                    parent_sequence,
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
            status_row = fetch_row(
                connection,
                "SELECT status FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            )
            if status_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            status = decode_row(status_row, SessionStatusRow)["status"]
            _assert_terminal_session_events_allowed(
                session_id=session_id,
                status=status,
                events=events,
            )
            leaf_row = fetch_row(
                connection,
                "SELECT leaf_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            )
            parent_sequence = None if leaf_row is None else decode_row(leaf_row, SessionLeafSequenceRow)["leaf_sequence"]
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
                            workspace_id, session_id, dedupe_key, delivered_at, event_sequence
                        ) SELECT ?, ?, ?, ?, last_event_sequence + 1 FROM sessions
                        WHERE workspace_id = ? AND session_id = ?
                        """,
                        (str(workspace), session_id, dedupe_key, delivered_at, str(workspace), session_id),
                    )
                    if inserted_delivery.rowcount == 0:
                        continue
                updated_at = self._next_timestamp(connection=connection)
                sequence_row = fetch_row(
                    connection,
                    """
                    UPDATE sessions
                    SET updated_at = ?, last_event_sequence = last_event_sequence + 1,
                        leaf_sequence = last_event_sequence + 1
                    WHERE workspace_id = ? AND session_id = ?
                    RETURNING last_event_sequence
                    """,
                    (updated_at, str(workspace), session_id),
                )
                if sequence_row is None:
                    raise UnknownSessionError(f"unknown session: {session_id}")
                sequence = decode_row(sequence_row, SessionLastEventSequenceRow)["last_event_sequence"]
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
                        workspace_id, session_id, sequence, parent_sequence, event_type, source, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(workspace),
                        event.session_id,
                        event.sequence,
                        parent_sequence,
                        event.event_type,
                        event.source,
                        json.dumps(event.payload, sort_keys=True),
                    ),
                )
                # Chain the batch: the next row follows this one. A skipped
                # dedupe duplicate never reaches here, so it does not perturb
                # either column.
                parent_sequence = sequence
                assigned.append(event)
            if interrupted_checkpoint is not None and assigned:
                checkpoint_metadata = interrupted_checkpoint.get("session_metadata")
                if not isinstance(checkpoint_metadata, dict):
                    raise ValueError("atomic checkpoint requires its canonical session metadata")
                # The canonical composition is stored on the row, not copied
                # into replay checkpoints; the shared merge reintroduced it.
                checkpoint_metadata = self._merge_runtime_owned_metadata(
                    connection=connection,
                    workspace=workspace,
                    session_id=session_id,
                    metadata=checkpoint_metadata,
                )
                checkpoint_metadata.pop("execution_composition", None)
                interrupted_checkpoint = {
                    **interrupted_checkpoint,
                    "session_metadata": checkpoint_metadata,
                }
                checkpoint_updated_at = self._next_timestamp(connection=connection)
                _ = connection.execute(
                    """
                    UPDATE sessions
                    SET status = 'interrupted', resume_checkpoint_json = ?, updated_at = ?
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                    (
                        json.dumps({**interrupted_checkpoint, "last_event_sequence": assigned[-1].sequence}, sort_keys=True),
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
        composition_ref: CompositionRef,
        composition: FrozenComposition | None = None,
        output: str | None = None,
        create_if_missing: bool = True,
        turn: int = 1,
        parent_session_id: str | None = None,
    ) -> None:
        """Persist a lightweight ``interrupted`` resume checkpoint to the sessions row.

        This is a cheap checkpoint of an in-flight run for resume-after-interrupt.
        Unlike ``save_run`` it does NOT write the ``session_events`` table, and does
        NOT write ``output`` / ``pending_approval_json`` / ``pending_question_json``
        (``output`` is only preserved, never overwritten with NULL on the update path).
        It writes exactly one ``sessions`` row — creating it on first call when
        ``create_if_missing`` is set (mandatory: ``append_session_events`` raises
        ``UnknownSessionError`` when the row is absent, so the row must exist
        before the first event append).

        ``parent_session_id`` is persisted on both the insert and update paths so
        a child session's first (un-sealed) row already carries its parent — the
        child must reference its parent even when the run ends before a terminal
        seal (``_write_session_snapshot`` is the only other writer of
        ``parent_session_id``, and it only runs at seal time).

        ``tool_results`` contains canonical ``ReportedCall`` payloads, serialized
        by ``_tool_results_from_events`` from the durable ``runtime.tool_completed``
        events. Each entry carries only ``tool_name``, ``status`` and
        ``reported_call``; resume decodes that report through the strict codec.
        """
        if type(composition_ref) is not CompositionRef or composition_ref.workspace != str(workspace):
            raise ValueError("checkpoint requires its exact workspace composition reference")
        if "execution_composition" in session_metadata:
            raise ValueError("canonical body must be supplied through the initial writer")
        session_metadata = {**session_metadata, "composition_ref": composition_ref.model_dump(mode="json")}
        if self._session_composition_ref(session_metadata) != composition_ref:
            raise ValueError("checkpoint composition reference disagrees with its metadata")
        if composition is not None:
            if type(composition) is not FrozenComposition:
                raise ValueError("initial composition must be a frozen canonical value")
            if composition_ref.owner != SessionCompositionOwner(kind="session", session_id=session_id):
                raise ValueError("initial session body requires its actual session owner")
            if composition.reference(workspace=str(workspace), owner=composition_ref.owner) != composition_ref:
                raise ValueError("initial session composition IDs disagree with its reference")
            session_metadata["execution_composition"] = composition.to_payload()
        persisted_metadata = session_metadata_for_persistence(session_metadata)
        checkpoint_json: str
        metadata_json: str
        with self._write_connect(workspace) as connection:
            existing = fetch_row(
                connection,
                "SELECT 1 FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            )
            if composition is not None:
                if existing is not None:
                    raise ValueError("canonical composition body can only be written with its initial row")
            else:
                self._load_execution_composition(connection, composition_ref)
            persisted_metadata = self._merge_runtime_owned_metadata(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                metadata=persisted_metadata,
                initial=existing is None,
            )
            checkpoint = self._interrupted_resume_checkpoint(
                prompt=prompt,
                session_metadata=persisted_metadata,
                tool_results=tool_results,
                last_event_sequence=last_event_sequence,
                output=output,
            )
            checkpoint_json = json.dumps(checkpoint, sort_keys=True)
            metadata_json = json.dumps(persisted_metadata, sort_keys=True)
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

    @staticmethod
    def _load_execution_composition(connection: sqlite3.Connection, ref: CompositionRef) -> FrozenComposition:
        if type(ref) is not CompositionRef:
            raise ValueError("canonical composition lookup requires a typed reference")
        if isinstance(ref.owner, SessionCompositionOwner):
            row = connection.execute(
                "SELECT metadata_json FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (ref.workspace, ref.owner.session_id),
            ).fetchone()
        elif isinstance(ref.owner, TaskCompositionOwner):
            row = connection.execute(
                "SELECT request_metadata_json FROM background_tasks WHERE workspace_id = ? AND task_id = ?",
                (ref.workspace, ref.owner.task_id),
            ).fetchone()
        else:
            raise ValueError("canonical composition owner is unsupported")
        if row is None:
            raise ValueError("canonical composition owner does not exist")
        metadata = json.loads(row[0])
        if not isinstance(metadata, dict):
            raise ValueError("canonical owner metadata must be an object")
        if CompositionRef.model_validate(metadata.get("composition_ref")) != ref:
            raise ValueError("canonical composition reference does not match its owner")
        frozen = FrozenComposition.from_payload(metadata.get("execution_composition"))
        if frozen.reference(workspace=ref.workspace, owner=ref.owner) != ref:
            raise ValueError("canonical composition IDs do not match their reference")
        return frozen

    def load_execution_composition(self, *, ref: CompositionRef) -> FrozenComposition:
        if type(ref) is not CompositionRef:
            raise ValueError("canonical composition lookup requires a typed reference")
        with self._connect(Path(ref.workspace)) as connection:
            return self._load_execution_composition(connection, ref)

    def export_session_bundle_rows(
        self,
        *,
        workspace: Path,
        session_ids: tuple[str, ...],
        task_ids: tuple[str, ...],
    ) -> dict[str, object]:
        """Read actual current rows, including append-only edges and dedupe truth."""
        result: dict[str, object] = {}
        with self._connect(workspace) as connection:
            for key, table, id_column, ids in (
                ("sessions", "sessions", "session_id", session_ids),
                ("events", "session_events", "session_id", session_ids),
                ("tasks", "background_tasks", "task_id", task_ids),
                ("deliveries", "session_event_deliveries", "session_id", session_ids),
            ):
                rows: list[dict[str, object]] = []
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    stored = connection.execute(
                        f"SELECT * FROM {table} WHERE workspace_id = ? AND {id_column} IN ({placeholders}) ORDER BY {id_column}",
                        (str(workspace), *ids),
                    ).fetchall()
                    for raw_row in stored:
                        row = dict(raw_row)
                        for column, value in row.items():
                            if column.endswith("_json") and value is not None:
                                row[column] = json.loads(value)
                        rows.append(row)
                result[key] = tuple(rows)
        return result

    def import_session_bundle_rows(
        self,
        *,
        workspace: Path,
        sessions: tuple[dict[str, object], ...],
        events: tuple[dict[str, object], ...],
        tasks: tuple[dict[str, object], ...],
        deliveries: tuple[dict[str, object], ...],
    ) -> None:
        """Insert a whole preflighted current closure in one lease-gated transaction."""
        groups = (
            ("sessions", sessions),
            ("background_tasks", tasks),
            ("session_events", events),
            ("session_event_deliveries", deliveries),
        )
        schema = cast(Any, self)._CANONICAL_SCHEMA
        encoded: dict[str, list[tuple[object, ...]]] = {}
        references: list[CompositionRef] = []
        session_ids = {row.get("session_id") for row in sessions}
        task_ids = {row.get("task_id") for row in tasks}
        if len(session_ids) != len(sessions) or len(task_ids) != len(tasks):
            raise ValueError("bundle contains duplicate owner rows")
        for table, rows in groups:
            columns = schema[table]
            values: list[tuple[object, ...]] = []
            keys: set[tuple[object, ...]] = set()
            primary_columns = tuple(column[0] for column in columns if column[4])
            for row in rows:
                if set(row) != {column[0] for column in columns} or row["workspace_id"] != str(workspace):
                    raise ValueError(f"bundle {table} row has an invalid current shape or workspace")
                primary = tuple(row[column] for column in primary_columns)
                if primary in keys:
                    raise ValueError(f"bundle {table} contains duplicate primary identities")
                keys.add(primary)
                serialized: list[object] = []
                for name, kind, required, _default, _primary in columns:
                    value = row[name]
                    if value is None:
                        if required:
                            raise ValueError(f"bundle {table}.{name} cannot be null")
                    elif name.endswith("_json"):
                        value = json.dumps(value, sort_keys=True, allow_nan=False)
                    elif kind == "TEXT" and type(value) is not str:
                        raise ValueError(f"bundle {table}.{name} must be text")
                    elif kind == "INTEGER" and type(value) is not int:
                        raise ValueError(f"bundle {table}.{name} must be an integer")
                    serialized.append(value)
                values.append(tuple(serialized))
                if table in ("sessions", "background_tasks"):
                    metadata = row["metadata_json" if table == "sessions" else "request_metadata_json"]
                    if not isinstance(metadata, dict):
                        raise ValueError("bundle owner metadata must be an object")
                    ref = (
                        self._session_composition_ref(metadata)
                        if table == "sessions"
                        else CompositionRef.model_validate(metadata.get("composition_ref"))
                    )
                    if ref.workspace != str(workspace):
                        raise ValueError("bundle owner reference names a different workspace")
                    body = metadata.get("execution_composition")
                    if body is not None:
                        frozen = FrozenComposition.from_payload(body)
                        actual_owner = (
                            SessionCompositionOwner(kind="session", session_id=cast(str, row["session_id"]))
                            if table == "sessions"
                            else TaskCompositionOwner(kind="task", task_id=cast(str, row["task_id"]))
                        )
                        if ref.owner != actual_owner or frozen.reference(workspace=str(workspace), owner=actual_owner) != ref:
                            raise ValueError("bundle canonical body does not belong to its genuine owner")
                    references.append(ref)
                elif row["session_id"] not in session_ids:
                    raise ValueError("bundle event or delivery names an absent imported session")
            encoded[table] = values
        with self._write_connect(workspace) as connection:
            for table, ids, column in (("sessions", session_ids, "session_id"), ("background_tasks", task_ids, "task_id")):
                for owner_id in ids:
                    if (
                        connection.execute(
                            f"SELECT 1 FROM {table} WHERE workspace_id = ? AND {column} = ?",
                            (str(workspace), owner_id),
                        ).fetchone()
                        is not None
                    ):
                        raise ValueError("bundle destination owner identity collided")
            for table, _rows in groups:
                names = tuple(column[0] for column in schema[table])
                placeholders = ",".join("?" for _ in names)
                connection.executemany(
                    f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})",
                    encoded[table],
                )
            for ref in references:
                self._load_execution_composition(connection, ref)
            for scope, rows in (("sessions", sessions), ("background_tasks", tasks)):
                maximum = max((cast(int, row["updated_at"]) for row in rows), default=0)
                connection.execute(
                    "INSERT INTO storage_sequences(scope, value) VALUES (?, ?) ON CONFLICT(scope) DO UPDATE SET value = MAX(value, excluded.value)",
                    (scope, maximum),
                )
            connection.commit()

    def restore_leaf_after_interrupted_resume(self, *, workspace: Path, session_id: str, sequence: int) -> None:
        """Restore the session's leaf to the interrupted run's safe position.

        An interrupted run leaves rows past its checkpoint that the resume
        re-runs, but under a tree those rows are not necessarily a dead run's
        output: after a checkout they are the *abandoned branch*, which must
        stay. Nothing is written, moved or deleted — the rows above the
        checkpoint stay in ``session_events``, off the current path, and a
        later checkout to one of them restores that branch.

        The watermark is deliberately NOT regressed to ``sequence``: it stays
        the append counter (``MAX(sequence)``), so recycled numbers can never
        make a ``parent_sequence``/``leaf_sequence`` pointing at a retained row
        resolve to a different row. Only the replay position moves.

        The call is unconditional on the resume path (including the ordinary
        no-checkout case): the leaf is set to the checkpoint's recorded
        position, which is the last durable event the run had reached.
        """
        with self._write_connect(workspace) as connection:
            _ = connection.execute(
                """
                UPDATE sessions
                SET leaf_sequence = ?
                WHERE workspace_id = ? AND session_id = ?
                """,
                (sequence, str(workspace), session_id),
            )
            connection.commit()

    def _session_metadata_and_events(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
    ) -> tuple[dict[str, object], tuple[EventEnvelope, ...]]:
        """Row metadata plus every stored event for one session, ascending sequence."""
        session_row = fetch_row(
            connection,
            """
                SELECT metadata_json
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
            (str(workspace), session_id),
        )
        if session_row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        event_rows = fetch_rows(
            connection,
            """
                SELECT sequence, event_type, source, payload_json
                FROM session_events
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY sequence ASC
                """,
            (str(workspace), session_id),
        )
        events = tuple(self._event_envelope_from_row(session_id=session_id, row=decode_row(row, SessionEventRow)) for row in event_rows)
        return (
            normalize_persisted_session_metadata(json.loads(decode_row(session_row, SessionMetadataRow)["metadata_json"])),
            events,
        )

    def newest_sequence_before(self, *, workspace: Path, session_id: str, sequence: int) -> int | None:
        """The newest stored entry on the current path with ``sequence < sequence``.

        This is the target of the position move a *revert to S* performs: the
        entry just before ``S``, which is where the session continues from.
        ``None`` when nothing precedes ``sequence`` on the path.
        """
        with self._connect(workspace) as connection:
            entries = self._session_tree_entries(connection=connection, workspace=workspace, session_id=session_id)
            leaf = self._leaf_sequence(connection=connection, workspace=workspace, session_id=session_id)
        if not entries:
            return None
        path = session_event_path(entries, target_sequence=None, leaf_sequence=leaf)
        below = [event.sequence for event in path if event.sequence < sequence]
        return max(below) if below else None

    @staticmethod
    def _tool_results_from_events(events: tuple[EventEnvelope, ...]) -> list[dict[str, object]]:
        tool_results: list[dict[str, object]] = []
        for event in events:
            if event.event_type != "runtime.tool_completed":
                continue
            report = parse_report_payload(event.payload.get("reported_call"))
            tool_results.append(
                {
                    "tool_name": report.final_tool_name,
                    "status": report.result.status,
                    "reported_call": report_payload(report),
                }
            )
        return tool_results

    def _session_tree_entries(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> tuple[SessionTreeEvent, ...]:
        """Read every stored event with its ancestor edge, ascending sequence."""
        rows = fetch_rows(
            connection,
            """
                SELECT sequence, parent_sequence, event_type, source, payload_json
                FROM session_events
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY sequence ASC
                """,
            (str(workspace), session_id),
        )
        return tuple(
            SessionTreeEvent(
                event=EventEnvelope(
                    session_id=session_id,
                    sequence=row["sequence"],
                    event_type=row["event_type"],
                    source=self._parse_event_source(row["source"]),
                    payload=json.loads(row["payload_json"]),
                ),
                parent_sequence=row["parent_sequence"],
            )
            for row in (decode_row(row, SessionTreeEventRow) for row in rows)
        )

    def _leaf_sequence(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int | None:
        row = fetch_row(
            connection,
            "SELECT leaf_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        return decode_row(row, SessionLeafSequenceRow)["leaf_sequence"]

    def checkout_session(self, *, workspace: Path, session_id: str, sequence: int) -> int:
        """Move the session's leaf to ``sequence`` — a pure position change.

        Nothing is written, moved, or deleted: the events after ``sequence``
        stay in ``session_events``, off the current path, and a later checkout
        to one of them restores that branch. Only the row's ``leaf_sequence``
        and the position-scoped cached state change.

        ``sequence`` must name a stored event. The root→target path must not
        split a tool call from its completion or an approval/question request
        from its resolution (the same rule a fork applies): resuming such a
        path would continue mid-interaction. The cached position — the context
        projection, todos and compaction marker in ``runtime_state`` plus the
        abandoned branch's pending approval/question — is dropped in the same
        ``BEGIN IMMEDIATE`` transaction as the move, so a later run re-derives
        it from the events on the new path.

        A checkout is an explicit "continue from here", so the session is never
        left terminal at the chosen position: the row becomes ``interrupted``,
        the resumable breakpoint status, and the stale resume checkpoint is
        replaced by one naming this position. Without this a checkout of a
        ``completed`` row would leave it sealed, and ``sessions resume`` would
        replay the stored response instead of continuing from the leaf the
        checkout just chose.

        The checkpoint's ``last_event_sequence`` is the checked-out position
        itself, NOT the row watermark: a resume restores the leaf to it, so the
        field means one thing — the position the interrupted resume continues
        from. The tail beyond it is the abandoned branch and is never deleted.
        """
        if sequence < 1:
            raise ValueError("checkout sequence must be a positive integer")
        with self._write_connect(workspace) as connection:
            metadata, _events = self._session_metadata_and_events(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
            )
            entries = self._session_tree_entries(connection=connection, workspace=workspace, session_id=session_id)
            if not any(entry.event.sequence == sequence for entry in entries):
                raise ValueError(f"session {session_id} has no event sequence {sequence}")
            try:
                path = session_event_path(entries, target_sequence=sequence)
            except SessionTreePathError as exc:
                raise ValueError(f"cannot checkout session {session_id}: {exc}") from exc
            dangling = _dangling_interaction(path)
            if dangling is not None:
                kind, label = dangling
                raise RuntimeSessionCheckoutBoundaryError(f"checkout target {sequence} splits a {kind} call from its result ({label})")
            next_metadata = {key: value for key, value in metadata.items() if key not in _NON_TRANSFERABLE_METADATA_KEYS}
            metadata_json = json.dumps(next_metadata, sort_keys=True)
            checkpoint = self._interrupted_resume_checkpoint(
                prompt=_checked_out_prompt(path),
                session_metadata=next_metadata,
                tool_results=(),
                last_event_sequence=sequence,
                output=None,
            )
            updated_at = self._next_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE sessions
                SET leaf_sequence = ?, metadata_json = ?, status = 'interrupted',
                    pending_approval_json = NULL, pending_question_json = NULL,
                    resume_checkpoint_json = ?, updated_at = ?
                WHERE workspace_id = ? AND session_id = ?
                """,
                (sequence, metadata_json, json.dumps(checkpoint, sort_keys=True), updated_at, str(workspace), session_id),
            )
            connection.commit()
            return sequence

    def session_path(self, *, workspace: Path, session_id: str, sequence: int | None = None) -> tuple[EventEnvelope, ...]:
        """Read-only root→leaf path for ``session_id`` at ``sequence`` (default: the leaf).

        A session with no events has the empty path: its unset leaf is a
        legitimate starting position, not a lost tree. An unset leaf with
        events present is still the corruption case the walk refuses.
        """
        with self._connect(workspace) as connection:
            entries = self._session_tree_entries(connection=connection, workspace=workspace, session_id=session_id)
            leaf = self._leaf_sequence(connection=connection, workspace=workspace, session_id=session_id)
        if not entries:
            return ()
        return session_event_path(entries, target_sequence=sequence, leaf_sequence=leaf)

    def session_entries(self, *, workspace: Path, session_id: str) -> tuple[SessionEntrySummary, ...]:
        """Read-only listing of every stored entry, ascending ``sequence``.

        Each row carries its ancestor edge and whether it is on the session's
        current root→leaf path, which is what a user needs to pick a checkout
        target: rows off the path are the branches a checkout left behind.

        A session with events but no leaf (an unset position over a non-empty
        log) is the same corruption ``session_path`` refuses, so the path walk
        raises here too rather than marking every row abandoned. A closed leaf
        that cannot be walked back to a root likewise raises: a listing that
        silently claims "everything is off-path" would hide the corruption the
        walk exists to catch. A session with no events lists nothing.
        """
        with self._connect(workspace) as connection:
            entries = self._session_tree_entries(connection=connection, workspace=workspace, session_id=session_id)
            leaf = self._leaf_sequence(connection=connection, workspace=workspace, session_id=session_id)
        if not entries:
            return ()
        path_sequences = {event.sequence for event in session_event_path(entries, target_sequence=None, leaf_sequence=leaf)}
        return tuple(
            SessionEntrySummary(
                sequence=entry.event.sequence,
                event_type=entry.event.event_type,
                parent_sequence=entry.parent_sequence,
                on_current_path=entry.event.sequence in path_sequences,
                preview=session_entry_preview(entry.event.event_type, entry.event.payload),
            )
            for entry in entries
        )

    def has_session(self, *, workspace: Path, session_id: str) -> bool:
        with self._connect(workspace) as connection:
            row = fetch_row(
                connection,
                """
                    SELECT 1
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                (str(workspace), session_id),
            )
        return row is not None

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponse:
        """Return ALL persisted events for a session, unfiltered.

        Boundary: storage returns every event — no compaction, no truncation, no
          context-window projection. The caller (or ``context/window.py``) decides what
        subset to present to the model, and replay walks the leaf path
          (``session_path``) rather than this flat log.
        """
        return self._load_session_response(
            workspace=workspace,
            session_id=session_id,
        )

    def load_session_status(self, *, workspace: Path, session_id: str) -> SessionStatus:
        """Return the persisted row status for a session.

        Lightweight read used by the runtime's terminal-seal guard
        (``VoidCodeRuntime._sealed_session_status``): the guard must inspect the
        durable status without materializing the full event log.
        """
        with self._connect(workspace) as connection:
            row = fetch_row(
                connection,
                "SELECT status FROM sessions WHERE workspace_id = ? AND session_id = ?",
                (str(workspace), session_id),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        return self._parse_session_status(decode_row(row, SessionStatusRow)["status"])

    def read_session_event_page(
        self,
        *,
        workspace: Path,
        session_id: str,
        after_sequence: int,
        limit: int,
        leaf_sequence: int | None = None,
    ) -> SessionEventPage:
        """Read a bounded page from a pinned root→leaf session path.

        The first call omits ``leaf_sequence`` to snapshot the current leaf.
        Pass the returned leaf on later calls so appends or checkouts do not
        change which branch the cursor traverses. ``max_sequence`` is the
        durable watermark and may exceed that pinned leaf.
        """
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool) or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("event page limit must be a positive integer")
        if leaf_sequence is not None and (not isinstance(leaf_sequence, int) or isinstance(leaf_sequence, bool) or leaf_sequence < 1):
            raise ValueError("leaf_sequence must be a positive integer")

        with self._connect(workspace) as connection:
            _ = connection.execute("BEGIN")
            state_row = fetch_row(
                connection,
                """
                SELECT leaf_sequence, last_event_sequence
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            )
            if state_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            state = decode_row(state_row, SessionEventPageStateRow)
            selected_leaf = state["leaf_sequence"] if leaf_sequence is None else leaf_sequence
            max_sequence = state["last_event_sequence"]
            if selected_leaf is None:
                if max_sequence != 0:
                    raise SessionTreePathError(f"session {session_id} has events but no leaf")
                if after_sequence != 0:
                    raise ValueError("after_sequence is not on the empty session path")
                connection.commit()
                return SessionEventPage(
                    leaf_sequence=None,
                    max_sequence=max_sequence,
                    entries=(),
                    next_after_sequence=None,
                )
            if selected_leaf > max_sequence:
                raise SessionTreePathError(f"session {session_id} leaf exceeds its event watermark")

            raw_rows = fetch_rows(
                connection,
                """
                WITH RECURSIVE path(sequence, parent_sequence, event_type, source, payload_json) AS (
                    SELECT sequence, parent_sequence, event_type, source, payload_json
                    FROM session_events
                    WHERE workspace_id = ? AND session_id = ? AND sequence = ?
                    UNION ALL
                    SELECT parent.sequence, parent.parent_sequence,
                           parent.event_type, parent.source, parent.payload_json
                    FROM session_events AS parent
                    JOIN path AS child
                      ON parent.workspace_id = ?
                     AND parent.session_id = ?
                     AND parent.sequence = child.parent_sequence
                    WHERE parent.sequence < child.sequence
                ),
                validation AS (
                    SELECT
                        EXISTS(SELECT 1 FROM path WHERE sequence = ?) AS leaf_found,
                        CASE WHEN ? = 0 THEN 1
                             ELSE EXISTS(SELECT 1 FROM path WHERE sequence = ?)
                        END AS cursor_found,
                        EXISTS(
                            SELECT 1
                            FROM path AS child
                            WHERE child.parent_sequence IS NOT NULL
                              AND NOT EXISTS(
                                  SELECT 1
                                  FROM session_events AS parent
                                  WHERE parent.workspace_id = ?
                                    AND parent.session_id = ?
                                    AND parent.sequence = child.parent_sequence
                                    AND parent.sequence < child.sequence
                              )
                        ) AS broken_path
                ),
                page AS (
                    SELECT sequence, parent_sequence, event_type, source, payload_json
                    FROM path
                    WHERE sequence > ?
                    ORDER BY sequence ASC
                    LIMIT ?
                )
                SELECT validation.leaf_found, validation.cursor_found, validation.broken_path,
                       page.sequence, page.parent_sequence, page.event_type, page.source, page.payload_json
                FROM validation
                LEFT JOIN page ON 1 = 1
                ORDER BY page.sequence ASC
                """,
                (
                    str(workspace),
                    session_id,
                    selected_leaf,
                    str(workspace),
                    session_id,
                    selected_leaf,
                    after_sequence,
                    after_sequence,
                    str(workspace),
                    session_id,
                    after_sequence,
                    limit + 1,
                ),
            )
            if not raw_rows:
                raise SessionTreePathError(f"session {session_id} event path could not be read")
            page_rows = [decode_row(row, SessionEventPageRow) for row in raw_rows]
            validation = page_rows[0]
            if validation["broken_path"]:
                raise SessionTreePathError(f"session {session_id} has broken event ancestry")
            if not validation["leaf_found"]:
                raise SessionTreePathError(f"session {session_id} has no event sequence {selected_leaf}")
            if not validation["cursor_found"]:
                raise ValueError(f"after_sequence {after_sequence} is not on the selected session path")

            stored_rows = [row for row in page_rows if row["sequence"] is not None]
            has_more = len(stored_rows) > limit
            entries = tuple(
                SessionTreeEvent(
                    event=EventEnvelope(
                        session_id=session_id,
                        sequence=cast(int, row["sequence"]),
                        event_type=cast(str, row["event_type"]),
                        source=self._parse_event_source(cast(str, row["source"])),
                        payload=cast(dict[str, object], json.loads(cast(str, row["payload_json"]))),
                    ),
                    parent_sequence=row["parent_sequence"],
                )
                for row in stored_rows[:limit]
            )
            next_after_sequence = entries[-1].event.sequence if has_more else None
            connection.commit()

        return SessionEventPage(
            leaf_sequence=selected_leaf,
            max_sequence=max_sequence,
            entries=entries,
            next_after_sequence=next_after_sequence,
        )

    def read_session_events_after(
        self,
        *,
        workspace: Path,
        session_id: str,
        after_sequence: int,
    ) -> SessionEventsAfter:
        """Return the flat durable event tail and row state for follow clients.

        One connection reads the row state (status + raw metadata) and performs
        one range scan on the ``(workspace_id, session_id, sequence)`` primary
        key. This preserves the existing follow-client contract; consumers
        needing bounded active-lineage reads use ``read_session_event_page``.
        Events come back undecorated for runtime policy projection.
        """
        with self._connect(workspace) as connection:
            session_row = fetch_row(
                connection,
                """
                    SELECT status, metadata_json
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                (str(workspace), session_id),
            )
            if session_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            session_state = decode_row(session_row, SessionStatusMetadataRow)
            metadata = cast(dict[str, object], json.loads(session_state["metadata_json"]))
            event_rows = fetch_rows(
                connection,
                """
                    SELECT sequence, event_type, source, payload_json
                    FROM session_events
                    WHERE workspace_id = ? AND session_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    """,
                (str(workspace), session_id, after_sequence),
            )
        return SessionEventsAfter(
            status=self._parse_session_status(session_state["status"]),
            metadata=metadata,
            events=tuple(self._event_envelope_from_row(session_id=session_id, row=decode_row(row, SessionEventRow)) for row in event_rows),
        )

    def _event_envelope_from_row(self, *, session_id: str, row: SessionEventRow) -> EventEnvelope:
        return EventEnvelope(
            session_id=session_id,
            sequence=row["sequence"],
            event_type=row["event_type"],
            source=self._parse_event_source(row["source"]),
            payload=json.loads(row["payload_json"]),
        )

    def rename_session(self, *, workspace: Path, session_id: str, title: str) -> None:
        """Set the user-settable title on an existing session row.

        The title is a row column the run snapshot does not own (see
        ``_write_session_snapshot``), so this is the only writer besides the
        carry-forward read. ``updated_at`` advances like every sibling session
        mutation, so a rename re-surfaces the session at the top of the listing.
        """
        with self._write_connect(workspace) as connection:
            updated = connection.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE workspace_id = ? AND session_id = ?",
                (title, self._next_timestamp(connection=connection), str(workspace), session_id),
            ).rowcount
            if updated != 1:
                raise UnknownSessionError(f"unknown session: {session_id}")
            connection.commit()

    def update_session_metadata(self, *, workspace: Path, session_id: str, metadata: dict[str, object]) -> None:
        """Persist a bounded snapshot without replacing queue-owned input."""
        persisted = session_metadata_for_persistence(metadata)
        with self._write_connect(workspace) as connection:
            persisted = self._merge_runtime_owned_metadata(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                metadata=persisted,
            )
            self._write_session_metadata_row(connection=connection, workspace=workspace, session_id=session_id, metadata=persisted)
            connection.commit()

    def enqueue_session_message(
        self,
        *,
        workspace: Path,
        session_id: str,
        content: str,
        kind: QueuedMessageKind,
        dedupe_key: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        with self._write_connect(workspace) as connection:
            metadata = self._read_session_message_metadata(connection=connection, workspace=workspace, session_id=session_id)
            updated = enqueue_runtime_message(metadata, content=content, kind=kind, dedupe_key=dedupe_key)
            self._write_session_metadata_row(
                connection=connection,
                workspace=workspace,
                session_id=session_id,
                metadata=session_metadata_for_persistence(updated),
            )
            connection.commit()
            pending = updated.get("pending_messages")
            return tuple(item for item in pending if isinstance(item, dict)) if isinstance(pending, list) else ()

    def drain_session_messages(
        self,
        *,
        workspace: Path,
        session_id: str,
        kind: QueuedMessageKind,
        remember_dedupe: bool = False,
    ) -> tuple[QueuedRuntimeMessage, ...]:
        with self._write_connect(workspace) as connection:
            metadata = self._read_session_message_metadata(connection=connection, workspace=workspace, session_id=session_id)
            updated, messages = drain_runtime_messages(metadata, kind=kind, remember_dedupe=remember_dedupe)
            if messages:
                self._write_session_metadata_row(
                    connection=connection,
                    workspace=workspace,
                    session_id=session_id,
                    metadata=session_metadata_for_persistence(updated),
                )
            connection.commit()
            return messages

    @staticmethod
    def _read_session_message_metadata(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> dict[str, object]:
        row = fetch_row(connection, "SELECT metadata_json FROM sessions WHERE workspace_id = ? AND session_id = ?", (str(workspace), session_id))
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        return normalize_persisted_session_metadata(json.loads(decode_row(row, SessionMetadataRow)["metadata_json"]))

    def _write_session_metadata_row(
        self,
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
        metadata: dict[str, object],
    ) -> None:
        updated = connection.execute(
            "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE workspace_id = ? AND session_id = ?",
            (json.dumps(metadata, sort_keys=True), self._next_timestamp(connection=connection), str(workspace), session_id),
        ).rowcount
        if updated != 1:
            raise UnknownSessionError(f"unknown session: {session_id}")

    def _load_session_response(
        self,
        *,
        workspace: Path,
        session_id: str,
    ) -> RuntimeResponse:
        """Load a session with ALL events from durable storage.

        Boundary: returns every stored event row unfiltered — no compaction,
        no truncation, no context-driven dropping. Under a tree the flat log
        holds the abandoned branches too, so replay is not built here: the
        path walk (``session_path``) decides what is replayable and which
        rows are on it. Both callers of this method want the whole log.
        """
        with self._connect(workspace) as connection:
            session_row = fetch_row(
                connection,
                """
                SELECT session_id, parent_session_id, status, turn, output, metadata_json
                FROM sessions
                WHERE workspace_id = ? AND session_id = ?
                """,
                (str(workspace), session_id),
            )
            if session_row is None:
                raise UnknownSessionError(f"unknown session: {session_id}")
            session_data = decode_row(session_row, SessionLoadRow)
            event_rows = fetch_rows(
                connection,
                """
                SELECT sequence, event_type, source, payload_json
                FROM session_events
                WHERE workspace_id = ? AND session_id = ?
                ORDER BY sequence ASC
                """,
                (str(workspace), session_id),
            )
        metadata = normalize_persisted_session_metadata(cast(dict[str, object], json.loads(session_data["metadata_json"])))
        session = SessionState(
            session=SessionRef(
                id=session_data["session_id"],
                parent_id=session_data["parent_session_id"],
            ),
            status=self._parse_session_status(session_data["status"]),
            turn=session_data["turn"],
            metadata=metadata,
        )
        events = tuple(self._event_envelope_from_row(session_id=session_id, row=decode_row(row, SessionEventRow)) for row in event_rows)
        output = session_data["output"]
        return RuntimeResponse(session=session, events=events, output=output)

    def load_session_result(self, *, workspace: Path, session_id: str) -> RuntimeSessionResult:
        response = self._load_session_response(
            workspace=workspace,
            session_id=session_id,
        )
        with self._connect(workspace) as connection:
            row = fetch_row(
                connection,
                """
                    SELECT prompt, title
                    FROM sessions
                    WHERE workspace_id = ? AND session_id = ?
                    """,
                (str(workspace), session_id),
            )
        if row is None:
            raise UnknownSessionError(f"unknown session: {session_id}")
        result_row = decode_row(row, SessionPromptTitleRow)
        prompt = result_row["prompt"]
        summary, error = self._result_summary(response=response, prompt=prompt)
        return RuntimeSessionResult(
            session=response.session,
            prompt=prompt,
            title=result_row["title"],
            status=response.session.status,
            summary=summary,
            output=response.output,
            error=error,
            transcript=response.events,
            last_event_sequence=response.events[-1].sequence if response.events else 0,
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
                    tool = str(event.payload["tool"])
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

    @staticmethod
    def _read_title(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> str | None:
        """Read the existing row title so ``INSERT OR REPLACE`` can carry it forward.

        ``_write_session_snapshot`` only knows about the run, not the user-set
        label, and an upsert rewrites every column: an absent row (first run of
        a session) reads ``None``, which is exactly the no-title state.
        """
        row = fetch_row(
            connection,
            "SELECT title FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is None:
            return None
        return decode_row(row, SessionTitleRow)["title"]

    @staticmethod
    def _read_fork_provenance(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        session_id: str,
    ) -> tuple[str | None, int | None]:
        """Read existing fork provenance so ``INSERT OR REPLACE`` can carry it.

        Same contract as ``_read_title``: the run snapshot does not own these
        columns, and an upsert rewrites every column. An absent row (first run
        of a non-forked session) reads ``(None, None)``, the no-provenance
        state.
        """
        row = fetch_row(
            connection,
            "SELECT forked_from_session_id, forked_at_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is None:
            return (None, None)
        provenance = decode_row(row, SessionForkProvenanceRow)
        return (provenance["forked_from_session_id"], provenance["forked_at_sequence"])

    def _read_created_at(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = fetch_row(
            connection,
            "SELECT created_at FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is not None:
            return decode_row(row, SessionCreatedAtRow)["created_at"]
        return self._next_auxiliary_timestamp(connection=connection)

    def _read_created_at_unix_ms(self, *, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int | None:
        row = fetch_row(
            connection,
            "SELECT created_at_unix_ms FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is None:
            return None
        return decode_row(row, SessionCreatedAtUnixMsRow)["created_at_unix_ms"]

    @staticmethod
    def _read_last_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = fetch_row(
            connection,
            "SELECT last_event_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is not None:
            return decode_row(row, SessionLastEventSequenceRow)["last_event_sequence"]
        return 0

    @staticmethod
    def _read_leaf_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int | None:
        """Read the persisted tree position so a row upsert can carry it forward.

        ``leaf_sequence`` is owned by the incremental append path (and by the
        fork insert); ``_write_session_snapshot``'s ``INSERT OR REPLACE`` only
        preserves it. An absent row (first run of a session) reads ``None``.
        """
        row = fetch_row(
            connection,
            "SELECT leaf_sequence FROM sessions WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        )
        if row is not None:
            return decode_row(row, SessionLeafSequenceRow)["leaf_sequence"]
        return None

    @staticmethod
    def _max_persisted_event_sequence(*, connection: sqlite3.Connection, workspace: Path, session_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM session_events WHERE workspace_id = ? AND session_id = ?",
            (str(workspace), session_id),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])
