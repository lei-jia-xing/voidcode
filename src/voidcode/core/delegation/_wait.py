"""Wait and join: entry-bound child work, wakeups, and completion acknowledgement."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError

from ...sdk.services import (
    BudgetExhaustion,
    ChildOutcome,
)
from ..child_contracts import ChildCompletionReceipt, ChildWaitDelivery
from ..events import CHILD_OUTCOME_RECEIPT_OWNER, EventType, child_completion_notice_id
from ..storage.store import (
    ContinuationStatus,
    SessionRecord,
    SessionStatus,
    StoreConflict,
    StoredEvent,
)
from ._cancellation import _DelegationCancellation


class _DelegationWait(_DelegationCancellation):
    """Wait and join: entry-bound child work, wakeups, and completion acknowledgement."""

    def _check_child_wait_interrupt(self, parent_session_id: str, admission_guard: Callable[[], bool] | None) -> None:
        self.core.ensure_open()
        if admission_guard is not None and not admission_guard():
            raise CancelledError("child wait was cancelled")
        if self.core.store.has_pending_user_message(parent_session_id):
            raise InterruptedError("child wait was interrupted by queued user input")

    def wait_children(
        self,
        parent_session_id: str,
        child_session_ids: Sequence[str],
        *,
        admission_guard: Callable[[], bool] | None = None,
    ) -> tuple[ChildWaitDelivery, ...]:
        """Deliver exact undelivered child facts, then wait for entry-bound work."""
        self.core.ensure_open()
        if isinstance(child_session_ids, (str, bytes)) or not isinstance(child_session_ids, Sequence):
            raise ValueError("child_session_ids must be a sequence of session ids")
        selected: list[str] = []
        seen_children: set[str] = set()
        for child_id in child_session_ids:
            if not isinstance(child_id, str) or not child_id:
                raise ValueError("child_session_ids must contain non-empty strings")
            if child_id in seen_children:
                continue
            self._owned_child(parent_session_id, child_id)
            seen_children.add(child_id)
            selected.append(child_id)
        if not selected:
            self._check_child_wait_interrupt(parent_session_id, admission_guard)
            return ()

        registrations = self.core.store.child_wait_registrations(tuple(selected))
        captured_messages: dict[str, set[str]] = {}
        bound_runs: set[tuple[str, str]] = set()
        initial_spawn_thresholds: dict[str, int] = {}
        approval_resume_thresholds: dict[str, int] = {}
        child_seen_sequences: dict[str, int] = {}
        parent_events = list(self.core.store.events(parent_session_id))
        parent_seen_sequence = max((event.sequence for event in parent_events), default=0)
        spawned_ids = {
            child_id
            for event in parent_events
            if event.event_type == EventType.CHILD_SPAWNED and isinstance((child_id := event.payload.get("child_session_id")), str)
        }

        def bind_child_events(child_id: str, events: Sequence[StoredEvent]) -> set[str]:
            started_message_ids: set[str] = set()
            continuation_runs = {event.run_id for event in events if event.event_type == EventType.CONTINUATION_STARTED and event.run_id}
            for event in events:
                if event.event_type == EventType.CONTINUATION_STARTED:
                    message_id = event.payload.get("continuation_id")
                    if isinstance(message_id, str) and message_id in captured_messages[child_id]:
                        started_message_ids.add(message_id)
                        if event.run_id:
                            bound_runs.add((child_id, event.run_id))
                elif (
                    event.event_type == EventType.USER_MESSAGE
                    and child_id in initial_spawn_thresholds
                    and event.sequence > initial_spawn_thresholds[child_id]
                    and event.run_id
                    and event.run_id not in continuation_runs
                ):
                    # The spawn fact reserves this first child run. A later queued
                    # follow-up is bound only by its own continuation identity.
                    bound_runs.add((child_id, event.run_id))
                    initial_spawn_thresholds.pop(child_id, None)
                elif (
                    event.event_type == EventType.RUN_RESUMED
                    and child_id in approval_resume_thresholds
                    and event.sequence > approval_resume_thresholds[child_id]
                    and event.run_id
                ):
                    # A public approval resume is a durable continuation of the exact
                    # waiting checkpoint captured at entry, with its actual new run id.
                    bound_runs.add((child_id, event.run_id))
            return started_message_ids

        started_messages: set[tuple[str, str]] = set()
        for child, message_ids in registrations:
            if child.parent_session_id != parent_session_id:
                raise PermissionError("child session does not belong to this parent session")
            captured_messages[child.session_id] = set(message_ids)
            if isinstance(child.run_id, str) and child.run_id:
                bound_runs.add((child.session_id, child.run_id))
            if child.status is SessionStatus.WAITING and isinstance(child.run_id, str) and child.run_id:
                approval_resume_thresholds[child.session_id] = child.last_event_sequence
            elif (
                child.run_id is None
                and child.session_id in spawned_ids
                and child.status
                not in (
                    SessionStatus.COMPLETED,
                    SessionStatus.FAILED,
                    SessionStatus.INTERRUPTED,
                )
            ):
                initial_spawn_thresholds[child.session_id] = child.last_event_sequence
            child_events = self.core.store.events(child.session_id)
            started_messages.update((child.session_id, message_id) for message_id in bind_child_events(child.session_id, child_events))
            child_seen_sequences[child.session_id] = max(
                child.last_event_sequence,
                max((event.sequence for event in child_events), default=0),
            )

        with self._child_finished_lock:
            generation = self._child_finished_generation
        selected_set = set(selected)
        while True:
            self._check_child_wait_interrupt(parent_session_id, admission_guard)
            for child_id in selected:
                new_events = self.core.store.events(child_id, after_sequence=child_seen_sequences[child_id])
                if new_events:
                    child_seen_sequences[child_id] = max(event.sequence for event in new_events)
                    started_messages.update((child_id, message_id) for message_id in bind_child_events(child_id, new_events))
            new_parent_events = self.core.store.events(parent_session_id, after_sequence=parent_seen_sequence)
            if new_parent_events:
                parent_events.extend(new_parent_events)
                parent_seen_sequence = max(event.sequence for event in new_parent_events)

            started_notice_ids = {
                notice_id
                for event in parent_events
                if event.event_type == EventType.CONTINUATION_STARTED and isinstance((notice_id := event.payload.get("continuation_id")), str)
            }
            deliveries: list[ChildWaitDelivery] = []
            for event in parent_events:
                if event.event_type != EventType.CHILD_FINISHED:
                    continue
                child_id = event.payload.get("child_session_id")
                run_id = event.payload.get("run_id")
                if child_id not in selected_set:
                    continue
                if not isinstance(child_id, str) or not isinstance(run_id, str) or not run_id:
                    raise StoreConflict("committed child completion fact has no exact child/run identity")
                if self.core.store.has_observer_receipt(parent_session_id, event.sequence, CHILD_OUTCOME_RECEIPT_OWNER):
                    continue
                notice_id = event.payload.get("notice_id")
                if notice_id is None and event.payload.get("notify_on_completion") is True:
                    notice_id = child_completion_notice_id(parent_session_id, child_id, run_id)
                if notice_id is not None and (not isinstance(notice_id, str) or not notice_id):
                    raise StoreConflict("committed child completion fact has an invalid notice identity")
                if notice_id is not None and (notice_id in started_notice_ids or self.core.store.continuation_started(parent_session_id, notice_id)):
                    continue
                status = event.payload.get("status")
                if status not in ("completed", "failed", "interrupted", "cancelled"):
                    raise StoreConflict(f"committed child completion fact has an unknown status: {status!r}")
                outcome = self._child_outcome(child_id, event.payload)
                if outcome.run_id != run_id:
                    raise StoreConflict("committed child completion fact changed its run identity")
                deliveries.append(
                    ChildWaitDelivery(
                        outcome=outcome,
                        source_session_id=parent_session_id,
                        event_sequence=event.sequence,
                        notice_id=notice_id,
                    )
                )

            for child_id in selected:
                child = self.core.store.session(child_id)
                run_id = child.run_id
                if child.status is not SessionStatus.INTERRUPTED or not isinstance(run_id, str) or not run_id:
                    continue
                if (child_id, run_id) not in bound_runs:
                    continue
                stop_event = self.core.store.latest_stop_event(child_id, run_id)
                if stop_event is None:
                    continue
                stop_cause = stop_event.payload.get("stop_cause")
                if stop_cause in ("owner_cancel", "user_stop"):
                    continue
                if self.core.store.has_observer_receipt(child_id, stop_event.sequence, CHILD_OUTCOME_RECEIPT_OWNER):
                    continue
                budget_payload = stop_event.payload.get("budget_exhaustion")
                budget_exhaustion = BudgetExhaustion.from_payload(budget_payload)
                if budget_payload is not None and budget_exhaustion is None:
                    raise StoreConflict("committed child stop has malformed budget exhaustion facts")
                reason = stop_event.payload.get("reason")
                outcome = ChildOutcome(
                    child_session_id=child_id,
                    status="interrupted",
                    error=reason if isinstance(reason, str) else None,
                    stop_cause=stop_cause if isinstance(stop_cause, str) else None,
                    run_id=run_id,
                    budget_exhaustion=budget_exhaustion,
                )
                deliveries.append(
                    ChildWaitDelivery(
                        outcome=outcome,
                        source_session_id=child_id,
                        event_sequence=stop_event.sequence,
                        notice_id=None,
                    )
                )
            if deliveries:
                self._check_child_wait_interrupt(parent_session_id, admission_guard)
                return tuple(deliveries)

            pending = False
            for child_id, run_id in bound_runs:
                child = self.core.store.session(child_id)
                if child.run_id != run_id:
                    continue
                if child.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
                    pending = True
                    break
                if child.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
                    if self._child_fact(parent_session_id, child_id, run_id) is None:
                        pending = True
                        break
                elif child.status is SessionStatus.INTERRUPTED:
                    stop_event = self.core.store.latest_stop_event(child_id, run_id)
                    if stop_event is None:
                        pending = True
                        break
                    stop_cause = stop_event.payload.get("stop_cause")
                    if stop_cause in ("owner_cancel", "user_stop") and self._child_fact(parent_session_id, child_id, run_id) is None:
                        pending = True
                        break
                elif child.status is not SessionStatus.IDLE:
                    raise StoreConflict(f"child session has an unknown wait status: {child.status.value!r}")
            if not pending:
                for child_id, message_ids in captured_messages.items():
                    child = self.core.store.session(child_id)
                    # A recoverably interrupted child cannot claim a follow-up until an
                    # explicit resume. Its durable message remains visible through the
                    # descendant projection, but it is not waitable current work; counting
                    # it would block every later wait with no legal boundary to claim it.
                    nonclaimable = child.status is SessionStatus.INTERRUPTED and not self.core.admits_fresh_work(child)
                    for message_id in message_ids:
                        if (child_id, message_id) in started_messages:
                            continue
                        status = self.core.store.continuation_status(message_id)
                        if status in (ContinuationStatus.PENDING, ContinuationStatus.CLAIMED):
                            if nonclaimable:
                                continue
                            pending = True
                            break
                        if status not in (
                            None,
                            ContinuationStatus.COMPLETED,
                            ContinuationStatus.FAILED,
                            ContinuationStatus.CANCELLED,
                        ):
                            raise StoreConflict(f"child message has an unknown continuation state: {status.value!r}")
                    if pending:
                        break
            if not pending:
                for child_id in initial_spawn_thresholds:
                    child = self.core.store.session(child_id)
                    if child.run_id is None and child.status not in (
                        SessionStatus.COMPLETED,
                        SessionStatus.FAILED,
                        SessionStatus.INTERRUPTED,
                    ):
                        pending = True
                        break
                    if (
                        child.status in (SessionStatus.COMPLETED, SessionStatus.FAILED)
                        and child.run_id
                        and self._child_fact(parent_session_id, child_id, child.run_id) is None
                    ):
                        pending = True
                        break
            self._check_child_wait_interrupt(parent_session_id, admission_guard)
            if not pending:
                # Rebind child-side reservations, starts, resumes, and stop events that
                # raced the live-state check before making an empty-work decision.
                child_events_changed = False
                for child_id in selected:
                    new_child_events = self.core.store.events(child_id, after_sequence=child_seen_sequences[child_id])
                    if new_child_events:
                        child_seen_sequences[child_id] = max(event.sequence for event in new_child_events)
                        started_messages.update((child_id, message_id) for message_id in bind_child_events(child_id, new_child_events))
                        child_events_changed = True
                if child_events_changed:
                    continue
                # A completion may commit after the first parent-event scan but before the
                # live-child check observes its terminal fact. Recheck the durable cursor
                # before deciding that no work is ready; notifications follow the commit.
                newer_parent_events = self.core.store.events(parent_session_id, after_sequence=parent_seen_sequence)
                if newer_parent_events:
                    parent_events.extend(newer_parent_events)
                    parent_seen_sequence = max(event.sequence for event in newer_parent_events)
                    continue
                with self._child_finished_lock:
                    self._check_child_wait_interrupt(parent_session_id, admission_guard)
                    if generation != self._child_finished_generation:
                        generation = self._child_finished_generation
                        continue
                    return ()
            with self._child_finished_lock:
                if generation != self._child_finished_generation:
                    generation = self._child_finished_generation
                    continue
                self._child_finished_lock.wait()
                generation = self._child_finished_generation

    def join_child(self, parent_session_id: str, child_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        """Wait for the child's CURRENT run; never satisfy a newer join with an older fact."""
        self.core.ensure_open()
        self._owned_child(parent_session_id, child_id)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._child_finished_lock:
            generation = self._child_finished_generation
        parent_seen_sequence: int | None = None

        def published_fact(run_id: str | None) -> dict[str, object] | None:
            """This run's published fact: memory cache first, then the parent history past its cursor."""
            nonlocal parent_seen_sequence
            fact = self._latest_child_fact(child_id, run_id)
            if fact is not None:
                return fact
            # The registry is only a cache: the durable parent fact is the restart truth.
            # Past the first scan only new parent events can carry an uncached publish:
            # a current run's fact cannot be evicted while it stays current, since 32
            # newer same-child publishes would have advanced the run past it.
            fresh = self.core.store.events(parent_session_id, after_sequence=parent_seen_sequence or 0)
            if fresh:
                parent_seen_sequence = max(event.sequence for event in fresh)
            elif parent_seen_sequence is not None:
                return None
            else:
                parent_seen_sequence = 0
            return self._durable_child_fact(parent_session_id, child_id, run_id)

        while True:
            child = self.core.store.session(child_id)
            if child.status is SessionStatus.WAITING:
                # Approval is not terminal and not something a parent wait can resolve.
                return self._child_status_outcome(child)
            if child.status is SessionStatus.INTERRUPTED and not self.core.admits_fresh_work(child):
                # A recoverable interruption needs an explicit public resume, not a join.
                # A terminal cancellation still publishes a fact (error/history); prefer it.
                fact = published_fact(child.run_id)
                if fact is not None:
                    return self._child_outcome(child_id, fact)
                with self._child_finished_lock:
                    publishing = child.run_id is not None and (child_id, child.run_id) in self._child_publication_inflight
                if publishing:
                    if deadline - time.monotonic() <= 0:
                        return self._child_status_outcome(child)
                    generation = self._await_child_wake(deadline, generation)
                    continue
                return self._child_status_outcome(child)
            if not self._child_has_outstanding_work(child):
                fact = published_fact(child.run_id)
                if fact is not None:
                    return self._child_outcome(child_id, fact)
                with self._child_finished_lock:
                    publishing = child.run_id is not None and (child_id, child.run_id) in self._child_publication_inflight
                terminal = child.status in (SessionStatus.COMPLETED, SessionStatus.FAILED)
                if not publishing and not terminal:
                    return self._child_status_outcome(child)
                # A terminal session whose fact/artifacts are still in flight is not a
                # completed outcome. Wait for the publisher; a deadline returns the honest
                # nonterminal wait outcome instead of completed/output=None.
            if deadline - time.monotonic() <= 0:
                return self._child_wait_outcome(child)
            generation = self._await_child_wake(deadline, generation)

    def _await_child_wake(self, deadline: float, generation: int) -> int:
        """Sleep until a child-state notify or the deadline; returns the observed generation.

        A notify that landed before this call is never slept through: the caller
        re-reads durable state and only sleeps on a still-current generation, so a
        wake is never lost and a spurious wake just re-reads unchanged state.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return generation
        with self._child_finished_lock:
            if generation != self._child_finished_generation:
                return self._child_finished_generation
            self._child_finished_lock.wait(timeout=remaining)
            return self._child_finished_generation

    def acknowledge_child_completion(self, parent_session_id: str, child_id: str, run_id: str) -> ChildCompletionReceipt | None:
        """Stage common receipt identity only for this run's durable terminal fact."""
        self.core.ensure_open()
        self._owned_child(parent_session_id, child_id)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        event = self._child_fact_event(parent_session_id, child_id, run_id)
        if event is None:
            return None
        status = event.payload.get("status")
        if status not in ("completed", "failed", "interrupted", "cancelled"):
            raise StoreConflict(f"committed child completion fact has an unknown status: {status!r}")
        fact_run = event.payload.get("run_id")
        if fact_run != run_id:
            raise StoreConflict("committed child completion fact changed its run identity")
        notice_id = event.payload.get("notice_id")
        if notice_id is None and event.payload.get("notify_on_completion") is True:
            notice_id = child_completion_notice_id(parent_session_id, child_id, run_id)
        if notice_id is not None and (not isinstance(notice_id, str) or not notice_id):
            raise StoreConflict("committed child completion fact has an invalid notice identity")
        if notice_id is not None and self.core.store.continuation_started(parent_session_id, notice_id):
            notice_id = None
        return ChildCompletionReceipt(
            source_session_id=event.session_id,
            event_sequence=event.sequence,
            notice_id=notice_id,
        )

    def _child_has_outstanding_work(self, child: SessionRecord) -> bool:
        """A running child, or a follow-up queued for it (never unread notices or parked work)."""
        if child.status is SessionStatus.RUNNING:
            return True
        if child.status is SessionStatus.IDLE:
            # A just-created spawn has not committed its first run event yet.
            return True
        if child.status not in (SessionStatus.COMPLETED, SessionStatus.FAILED):
            return False
        for continuation in self.core.store.pending_continuations(child.session_id, limit=50):
            if continuation.payload.get("kind") == "child_followup":
                return True
        return False
