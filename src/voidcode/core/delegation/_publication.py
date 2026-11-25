"""Exactly-once terminal publication: child facts, history artifacts, and notices."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping

from ...sdk.hooks import StopCause
from ...sdk.services import (
    BudgetExhaustion,
)
from ..child_contracts import ChildFinishedNoticePayload, ChildRunStatus
from ..events import CHILD_MESSAGE_OWNER, EventType, child_completion_notice_id
from ..history_renderer import iter_event_blocks
from ..storage.store import (
    EventDraft,
    SequenceConflict,
    StagedContinuationDraft,
    StoreConflict,
    StoredEvent,
)
from ._base import (
    _CHILD_FACT_BACKOFF_BASE_SECONDS,
    _CHILD_FACT_BACKOFF_CAP_SECONDS,
    _CHILD_FACT_MAX_ATTEMPTS,
    CHILD_INLINE_OUTPUT_BYTES,
    CHILD_OUTPUT_PREVIEW_BYTES,
)
from ._delivery import _DelegationDelivery
from ._outcomes import _utf8_preview


class _DelegationPublication(_DelegationDelivery):
    """Exactly-once terminal publication: child facts, history artifacts, and notices."""

    def _publish_child_terminal(
        self,
        parent_session_id: str,
        child_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        output: str | None,
        error: str | None,
        stop_cause: StopCause | None,
        label: str,
        notify: bool,
        deliver: bool,
        budget_exhaustion: BudgetExhaustion | None = None,
        output_error: str | None = None,
    ) -> None:
        """Publish one terminal child run exactly once: fact + immutable history + notice.

        A waiting join returns the outcome only after the durable fact commits and the outcome is
        remembered; the durable fact is the restart truth, and a recovered publication is
        idempotent. Publication is claimed per (child, run).
        """
        if not run_id or status not in ("completed", "failed", "interrupted", "cancelled"):
            return
        key = (child_id, run_id)
        # A concurrent publication for this run is already working: join it instead of
        # silently skipping, so the caller never observes "done" before the fact lands.
        deadline = time.monotonic() + 30.0
        with self._child_finished_lock:
            while key in self._child_publication_inflight and time.monotonic() < deadline:
                self._child_finished_lock.wait(timeout=0.2)
            already_done = key in self._published_child_runs
            if not already_done:
                self._published_child_runs.add(key)
                self._child_publication_inflight.add(key)
                self._child_finished_lock.notify_all()
                self._child_finished_generation += 1
        claimed_here = not already_done
        try:
            existing = self._child_fact(parent_session_id, child_id, run_id)
            if existing is not None:
                self._remember_child_outcome(child_id, run_id, existing)
                return
            if already_done:
                # Winner still working past the deadline (or lost the race after it):
                # never double-commit; retry paths observe the terminal child again.
                return
            resolved_label = label or self._child_label(parent_session_id, child_id)
            notice_root_id: str | None = None
            notice_id: str | None = None
            if notify:
                notice_root_id = self._captured_child_root_id(child_id)
                notice_id = child_completion_notice_id(parent_session_id, child_id, run_id)
            payload = self._child_fact_payload(
                child_id,
                run_id=run_id,
                status=status,
                output=output,
                error=error,
                stop_cause=stop_cause,
                label=resolved_label,
                notify=notify,
                notice_id=notice_id,
                budget_exhaustion=budget_exhaustion,
                output_error=output_error,
            )
            notice = None
            if notify and notice_id is not None and notice_root_id is not None:
                notice = StagedContinuationDraft(
                    continuation_id=notice_id,
                    owner=CHILD_MESSAGE_OWNER,
                    source="core",
                    payload=self._child_notice_payload(
                        child_id,
                        run_id=run_id,
                        status=status,
                        stop_cause=stop_cause,
                        label=resolved_label,
                        output=output,
                        error=error,
                        payload=payload,
                        notice_id=notice_id,
                    ),
                    idempotency_key=f"child-finished:{child_id}:{run_id}",
                    ordinal=0,
                    root_id=notice_root_id,
                )
            committed = self._commit_child_fact(
                parent_session_id,
                EventDraft(EventType.CHILD_FINISHED, "core", "plugin", payload),
                notice,
            )
            if not committed:
                # No durable fact means no publishable outcome. Drop the in-process claim so
                # the next join/recovery path can retry instead of serving a cache-only result.
                with self._child_finished_lock:
                    self._published_child_runs.discard(key)
                return
            self._remember_child_outcome(child_id, run_id, payload)
            if deliver and notice_id is not None:
                self._deliver_child_notice(parent_session_id, notice_id)
        except Exception:
            # A failed publication is retried by the next path that observes the terminal child
            # (startup recovery, a later join, or a new run). Never lose the claim for retries.
            with self._child_finished_lock:
                if claimed_here:
                    self._published_child_runs.discard(key)
            raise
        finally:
            with self._child_finished_lock:
                if claimed_here:
                    self._child_publication_inflight.discard(key)
                self._child_finished_lock.notify_all()
                self._child_finished_generation += 1

    def _child_fact_payload(
        self,
        child_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        output: str | None,
        error: str | None,
        stop_cause: StopCause | None,
        label: str,
        notify: bool,
        notice_id: str | None,
        budget_exhaustion: BudgetExhaustion | None,
        output_error: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": child_id,
            "run_id": run_id,
            "status": status,
            "error": error,
            "stop_cause": stop_cause,
            "label": label,
            "notify_on_completion": notify,
        }
        if budget_exhaustion is not None:
            payload["budget_exhaustion"] = budget_exhaustion.as_payload()
        if notice_id is not None:
            payload["notice_id"] = notice_id
        if output_error is not None:
            payload["output_error"] = output_error
        if output is not None:
            encoded = output.encode("utf-8")
            payload["output_bytes"] = len(encoded)
            if len(encoded) <= CHILD_INLINE_OUTPUT_BYTES:
                payload["output"] = output
            else:
                payload["output_preview"] = _utf8_preview(output, CHILD_OUTPUT_PREVIEW_BYTES)
                try:
                    reference = self._spill_child_output(output)
                except Exception as exc:
                    payload["output_error"] = f"{type(exc).__name__}: {exc}"
                else:
                    payload["output_sha256"] = reference["sha256"]
                    payload["output_bytes"] = reference["bytes"]
        else:
            payload["output"] = None
        try:
            run_type_events = self.core.store.events_by_types(child_id, (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED))
            # The snapshot ends exactly at this run's genuine terminal event (prior history
            # included). A late control/progress row tagged with the same run id must not extend
            # an immutable snapshot past the terminal commit.
            run_events = [event for event in run_type_events if event.run_id == run_id]
            terminal = self._terminal_child_event(run_events)
            if terminal is None:
                raise ValueError(f"no genuine terminal event for child run {run_id!r}; refusing a whole-session snapshot")
            items = self.core.store.events(child_id, limit=terminal[1].sequence)
            header = (
                f"# Child session {child_id}\n\n"
                f"- run: {run_id or '(none)'}\n"
                f"- status: {status}\n"
                f"- label: {label or '(none)'}\n"
                f"- events: {len(items)}\n"
                f"- live view (paged): transcript://{child_id}\n\n"
            )
            spill_id, spill = self.core._artifacts.start_spill("text/markdown; charset=utf-8", owner="core")
            try:
                spill.append(header.encode("utf-8"))
                for index, block in enumerate(iter_event_blocks(items)):
                    separator = "\n\n" if index else ""
                    spill.append(f"{separator}{block}".encode())
                sha256, size, media_type = self.core._artifacts.finish(spill_id)
            except Exception:
                self.core._artifacts.discard(spill_id)
                raise
            payload["history"] = {
                "sha256": sha256,
                "bytes": size,
                "media_type": media_type,
                "events": len(items),
            }
        except Exception as exc:
            payload["history_error"] = f"{type(exc).__name__}: {exc}"
        return payload

    def _child_notice_payload(
        self,
        child_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        stop_cause: StopCause | None,
        label: str,
        output: str | None,
        error: str | None,
        payload: Mapping[str, object],
        notice_id: str,
    ) -> dict[str, object]:
        preview = payload.get("output_preview")
        if preview is None and output is not None:
            preview = _utf8_preview(output, CHILD_OUTPUT_PREVIEW_BYTES)
        raw_error = _utf8_preview(error, CHILD_OUTPUT_PREVIEW_BYTES) if error is not None else None
        output_error = payload.get("output_error")
        history_error = payload.get("history_error")
        output_sha256 = payload.get("output_sha256")
        history = payload.get("history")
        history_sha256 = history.get("sha256") if isinstance(history, Mapping) else None
        output_bytes = payload.get("output_bytes")
        notice = ChildFinishedNoticePayload(
            notice_id=notice_id,
            child_session_id=child_id,
            run_id=run_id,
            label=label,
            status=status,
            stop_cause=stop_cause,
            output_bytes=output_bytes if isinstance(output_bytes, int) else None,
            preview=str(preview) if preview is not None else None,
            output_sha256=str(output_sha256) if output_sha256 is not None else None,
            history_sha256=str(history_sha256) if history_sha256 is not None else None,
            output_error=str(output_error) if isinstance(output_error, str) and output_error else None,
            history_error=str(history_error) if isinstance(history_error, str) and history_error else None,
            error=raw_error,
        )
        return notice.to_mapping()

    def _spill_child_output(self, text: str) -> dict[str, object]:
        spill_id, spill = self.core._artifacts.start_spill("text/plain; charset=utf-8", owner="core")
        try:
            spill.append(text.encode("utf-8"))
            sha256, size, _media_type = self.core._artifacts.finish(spill_id)
        except Exception:
            self.core._artifacts.discard(spill_id)
            raise
        return {"sha256": sha256, "bytes": size}

    def _commit_child_fact(
        self,
        parent_session_id: str,
        event: EventDraft,
        notice: StagedContinuationDraft | None,
    ) -> bool:
        """Commit the fact and its notice atomically against the parent's fresh tip.

        Only a moved tip is retried, with bounded attempts and jittered backoff; a
        parent waiting in join must never be able to deadlock a child, so anything
        else fails fast and the next join/recovery path retries. The durable terminal
        child session remains for recovery if this process exits before the commit.
        """
        for attempt in range(1, _CHILD_FACT_MAX_ATTEMPTS + 1):
            if self.core._closed:
                return False
            session = self.core.store.session(parent_session_id)
            if session.active_revision_id != self.core.revision_id:
                return False
            try:
                self.core.commit_public_staged(
                    parent_session_id,
                    events=(event,),
                    revision_id=self.core.revision_id,
                    staged_continuations=(notice,) if notice is not None else (),
                )
                return True
            except SequenceConflict:
                if attempt >= _CHILD_FACT_MAX_ATTEMPTS or self.core._closed:
                    return False
                time.sleep(random.uniform(0.0, min(_CHILD_FACT_BACKOFF_CAP_SECONDS, _CHILD_FACT_BACKOFF_BASE_SECONDS * (2.0 ** (attempt - 1)))))
            except StoreConflict:
                return False
        return False

    def _child_fact(self, parent_session_id: str, child_id: str, run_id: str) -> dict[str, object] | None:
        finished = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_FINISHED,))
        for event in reversed(finished):
            if event.event_type != EventType.CHILD_FINISHED or event.payload.get("child_session_id") != child_id:
                continue
            fact_run = event.payload.get("run_id")
            if not isinstance(fact_run, str) or not fact_run:
                raise StoreConflict(f"committed child completion fact for {child_id!r} has no exact run identity")
            if fact_run == run_id:
                return dict(event.payload)
            # Another run's fact proves nothing about this run: keep scanning older facts.
            continue
        return None

    def _child_fact_event(self, parent_session_id: str, child_id: str, run_id: str) -> StoredEvent | None:
        """Find one exact terminal result event, never a current-session or latest-output substitute."""
        finished = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_FINISHED,))
        for event in reversed(finished):
            if event.event_type != EventType.CHILD_FINISHED or event.payload.get("child_session_id") != child_id:
                continue
            if event.payload.get("run_id") == run_id:
                return event
        return None

    def _durable_child_fact(self, parent_session_id: str, child_id: str, run_id: str | None) -> dict[str, object] | None:
        """Load one run's published fact from durable parent events; the registry is only a cache."""
        if run_id is None:
            return None
        payload = self._child_fact(parent_session_id, child_id, run_id)
        if payload is not None:
            self._remember_child_outcome(child_id, run_id, payload)
        return payload

    def _child_fact_exists(self, parent_session_id: str, child_id: str, run_id: str) -> bool:
        return self._child_fact(parent_session_id, child_id, run_id) is not None
