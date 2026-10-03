from __future__ import annotations

from pathlib import Path
from typing import cast

from ..core.event_store import FactEntry, FactPage
from ..core.turns import CallSeed, ToolCompletedFact, TurnFact, normalize_call_result
from ..security.redaction import redact_mapping
from ..tools.contracts import ToolResult
from .events import EventEnvelope
from .execution.resume_checkpoint import tool_results_from_checkpoint, validated_resume_checkpoint_envelope
from .execution.tool_result_projection import _serialized_tool_results
from .execution.turn_recovery import persisted_turn_batch, restored_turn_batch
from .fact_codec import FACT_CODEC_VERSION, decode_fact, encode_fact, require_fact_codec_version
from .session import SessionState, session_metadata_for_persistence
from .session_metadata_helpers import runtime_state_value, session_metadata_with_runtime_state_updates
from .storage.ports import SessionEventRepository, SessionRecoveryRepository, SessionRepository


class SqliteFactStore:
    """Typed fact view over the existing guarded event/branch owners."""

    def __init__(
        self,
        *,
        events: SessionEventRepository,
        workspace: Path,
        session_id: str,
        branches: SessionRepository | None = None,
        recovery: SessionRecoveryRepository | None = None,
        session: SessionState | None = None,
        codec_version: int = FACT_CODEC_VERSION,
    ) -> None:
        require_fact_codec_version(codec_version)
        if session is not None and session.session.id != session_id:
            raise ValueError("fact projection session does not match the bound log")
        self._events = events
        self._workspace = workspace
        self._session_id = session_id
        self._branches = branches
        self._recovery = recovery
        self._session = session
        self._codec_version = codec_version

    def append(self, facts: tuple[TurnFact, ...], *, dedupe_keys: tuple[str | None, ...] = ()) -> tuple[int, ...]:
        return tuple(row.sequence for row in self.append_for_publication(facts, dedupe_keys=dedupe_keys))

    def append_for_publication(
        self,
        facts: tuple[TurnFact, ...],
        *,
        dedupe_keys: tuple[str | None, ...] = (),
        interrupted_checkpoint: dict[str, object] | None = None,
    ) -> tuple[EventEnvelope, ...]:
        if dedupe_keys and len(dedupe_keys) != len(facts):
            raise ValueError("each fact requires its own dedupe slot")
        encoded = tuple(encode_fact(fact, version=self._codec_version, session=self._session) for fact in facts)
        if any(not fact.persistable for fact in encoded):
            raise ValueError("provider stream and tool-call deltas are live-only facts")
        native_checkpoint = self._native_checkpoint(facts)
        if native_checkpoint is not None:
            if interrupted_checkpoint is not None:
                raise ValueError("native completion and caller checkpoint cannot compete for the same atomic snapshot")
            interrupted_checkpoint = native_checkpoint
        return self._events.append_session_events(
            workspace=self._workspace,
            session_id=self._session_id,
            events=tuple(
                (fact.event_type, fact.source, fact.payload, dedupe_keys[index] if dedupe_keys else None) for index, fact in enumerate(encoded)
            ),
            interrupted_checkpoint=interrupted_checkpoint,
        )

    def _native_checkpoint(self, facts: tuple[TurnFact, ...]) -> dict[str, object] | None:
        completions = tuple(fact for fact in facts if isinstance(fact, ToolCompletedFact) and fact.batch is not None)
        if not completions:
            return None
        if self._recovery is None:
            raise ValueError("authentic native snapshots require the narrow recovery role")
        checkpoint = validated_resume_checkpoint_envelope(
            checkpoint=self._recovery.load_resume_checkpoint(workspace=self._workspace, session_id=self._session_id),
            expected_kind="interrupted",
        ).payload
        metadata = checkpoint.get("session_metadata")
        results = checkpoint.get("tool_results")
        if not isinstance(metadata, dict) or not isinstance(results, list):
            raise ValueError("native snapshot requires the existing interrupted checkpoint metadata/results")
        tool_results_from_checkpoint(results)
        metadata = self._session.metadata if self._session is not None else metadata
        prior_leaf = self._events.read_session_event_page(
            workspace=self._workspace,
            session_id=self._session_id,
            after_sequence=0,
            limit=1,
        ).leaf_sequence
        serialized = list(results)
        for fact in completions:
            batch = fact.batch
            assert batch is not None
            if batch.completed_results:
                raise ValueError("a factual batch snapshot cannot duplicate completed results")
            raw = runtime_state_value(metadata, "turn_batch")
            candidate = persisted_turn_batch(
                batch,
                session_id=self._session_id,
                run_id=None,
                started_sequence=prior_leaf or 0,
            )
            same_batch = isinstance(raw, dict) and all(raw.get(key) == candidate[key] for key in ("calls", "reasoning", "run_step"))
            completed_ids: tuple[str, ...] = ()
            if same_batch:
                assert isinstance(raw, dict)
                ids = raw.get("completed_call_ids")
                started = raw.get("started_sequence")
                run_id = raw.get("run_id")
                if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                    raise ValueError("recorded native completion identities are malformed")
                if not isinstance(started, int) or isinstance(started, bool) or started < 0:
                    raise ValueError("recorded native batch has no authentic start cursor")
                if run_id is not None and not isinstance(run_id, str):
                    raise ValueError("recorded native batch has an invalid run identity")
                completed_ids = tuple(cast(list[str], ids))
                candidate = persisted_turn_batch(
                    batch, session_id=self._session_id, run_id=run_id, started_sequence=started, completed_call_ids=completed_ids
                )
            result = normalize_call_result(fact.call, fact.result)
            call_id = fact.call.tool_call_id
            assert call_id is not None
            if call_id not in completed_ids:
                completed_ids = (*completed_ids, call_id)
                serialized.extend(_serialized_tool_results((result,)))
            candidate["completed_call_ids"] = list(completed_ids)
            if completed_ids != tuple(call.tool_call_id for call in batch.calls)[: len(completed_ids)]:
                raise ValueError("actual native completion is not the next original batch call")
            metadata = session_metadata_with_runtime_state_updates(metadata, updates={"turn_batch": candidate})
        return {
            **checkpoint,
            "session_metadata": session_metadata_for_persistence(metadata),
            "tool_results": [redact_mapping(result) for result in serialized],
        }

    def restore_batch(self) -> CallSeed:
        if self._recovery is None:
            raise ValueError("this fact view was not given native recovery authority")
        checkpoint = validated_resume_checkpoint_envelope(
            checkpoint=self._recovery.load_resume_checkpoint(workspace=self._workspace, session_id=self._session_id),
            expected_kind="interrupted",
        ).payload
        metadata = checkpoint.get("session_metadata")
        if not isinstance(metadata, dict):
            raise ValueError("native continuation has no canonical checkpoint metadata")
        raw = runtime_state_value(metadata, "turn_batch")
        if not isinstance(raw, dict):
            raise ValueError("legacy facts have no authentic native snapshot; migrate before continuing")
        started = raw.get("started_sequence")
        if not isinstance(started, int) or isinstance(started, bool) or started < 0:
            raise ValueError("native continuation has no authentic start cursor")
        results: list[ToolResult] = []
        cursor, leaf = started, None
        while True:
            page = self.read(after_sequence=cursor, limit=100, leaf_sequence=leaf)
            leaf = page.leaf_sequence
            results.extend(entry.fact.result for entry in page.entries if isinstance(entry.fact, ToolCompletedFact))
            if page.next_after_sequence is None:
                break
            cursor = page.next_after_sequence
        batch, _ = restored_turn_batch(metadata, session_id=self._session_id, tool_results=results)
        return batch

    def read(self, *, after_sequence: int = 0, limit: int, leaf_sequence: int | None = None) -> FactPage:
        page = self._events.read_session_event_page(
            workspace=self._workspace,
            session_id=self._session_id,
            after_sequence=after_sequence,
            limit=limit,
            leaf_sequence=leaf_sequence,
        )
        entries: list[FactEntry] = []
        for entry in page.entries:
            fact = decode_fact(entry.event, version=self._codec_version)
            if fact is not None:
                entries.append(FactEntry(entry.event.sequence, entry.parent_sequence, fact))
        return FactPage(page.leaf_sequence, page.max_sequence, tuple(entries), page.next_after_sequence)

    def checkout(self, sequence: int) -> None:
        if self._branches is None:
            raise ValueError("this fact view was not given branch mutation authority")
        self._branches.checkout_session(workspace=self._workspace, session_id=self._session_id, sequence=sequence)

    def fork(self, sequence: int | None = None) -> SqliteFactStore:
        if self._branches is None:
            raise ValueError("this fact view was not given fork authority")
        branch = self._branches.fork_session(workspace=self._workspace, session_id=self._session_id, at_sequence=sequence)
        return SqliteFactStore(
            events=self._events,
            workspace=self._workspace,
            session_id=branch.session.id,
            branches=self._branches,
            recovery=self._recovery,
            codec_version=self._codec_version,
        )
