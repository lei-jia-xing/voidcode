"""Cancellation fences, reserved-child claims, and subtree cancellation."""

from __future__ import annotations

from ...sdk.services import (
    CancelSubtreeReceipt,
)
from ..cancellation import RUN_BOUND
from ..events import CHILD_MESSAGE_OWNER, EventType
from ..storage.store import (
    CheckpointKind,
    EventDraft,
    SessionRecord,
    SessionStatus,
    StoreConflict,
)
from ._spawn import _DelegationSpawn


class _DelegationCancellation(_DelegationSpawn):
    """Cancellation fences, reserved-child claims, and subtree cancellation."""

    def cancel_descendant_subtree(
        self, bound_session_id: str, target_session_id: str, *, expected_run_id: str | None, reason: str = "owner cancellation"
    ) -> CancelSubtreeReceipt:
        self.core.ensure_open()
        bound = self.core.session(bound_session_id)
        try:
            target = self.core.store.session(target_session_id)
        except KeyError as exc:
            raise PermissionError("cancellation target is not a descendant of this session") from exc
        if target.workspace != bound.workspace:
            raise PermissionError("cancellation target is not a descendant of this session")
        chain = self._parent_chain(target)
        if bound_session_id not in chain:
            raise PermissionError("cancellation target is not a descendant of this session")
        if (target.run_id or None) != expected_run_id:
            raise ValueError("cancellation target moved to a newer run; refresh and confirm again")
        # Registration takes store -> lifecycle. Install the fence without
        # reading the store under lifecycle, then census committed admissions.
        with self._lifecycle_lock:
            self._cancel_fences[target_session_id] = self._cancel_fences.get(target_session_id, 0) + 1
        try:
            descendants = self.core.store.descendant_sessions(target_session_id)
            if (self.core.store.session(target_session_id).run_id or None) != expected_run_id:
                raise ValueError("cancellation target moved to a newer run; refresh and confirm again")
            subtree = (target_session_id, *(child.session_id for child in descendants))
            observed_runs = {child.session_id: child.run_id or None for child in descendants}
            observed_runs[target_session_id] = expected_run_id
            cancelled: list[str] = []
            already_terminal: list[str] = []
            disarmed: list[str] = []
            claimed: list[str] = []
            moved: list[str] = []
            for node_id in reversed(subtree):
                record = self.core.store.session(node_id)
                observed_run = observed_runs.get(node_id)
                if (record.run_id or None) != observed_run:
                    moved.append(node_id)
                    continue
                # Disarm first so an already-enqueued follow-up cannot start a
                # new run after the snapshot below. The fence blocks new
                # enqueues; a newer run that still slips through is left alone
                # and reported distinctly below. No session lock is taken here:
                # a live run holds its own lock for the whole provider call,
                # and the bound signal below binds inside the abort registry.
                pending = self.core.store.cancel_pending_continuations(node_id, CHILD_MESSAGE_OWNER, expected_session=record)
                fresh = self.core.store.session(node_id)
                fresh_run = fresh.run_id or None
                if fresh_run != observed_run:
                    if pending:
                        disarmed.append(node_id)
                    moved.append(node_id)
                    continue
                if fresh.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
                    if pending:
                        disarmed.append(node_id)
                    else:
                        already_terminal.append(node_id)
                    continue
                if record.run_id is None and record.status is SessionStatus.IDLE:
                    # Reserved but unstarted: the lock-free CAS claim below wins
                    # only on the pristine state, so a racing first run either
                    # claims first (claim is moot) or loses its own CAS.
                    if self._claim_reserved_child(node_id, reason):
                        claimed.append(node_id)
                        continue
                    if (self.core.store.session(node_id).run_id or None) != observed_run:
                        moved.append(node_id)
                        continue
                stopped = self.core._cancel_session_run(node_id, reason, cause="owner_cancel", target=RUN_BOUND(observed_run))
                if not stopped and (self.core.store.session(node_id).run_id or None) != observed_run:
                    # The bound signal refused: a newer run claimed after the
                    # pre-signal read. It was never signalled; report distinctly.
                    moved.append(node_id)
                    continue
                if pending and not stopped:
                    disarmed.append(node_id)
                elif stopped:
                    cancelled.append(node_id)
                else:
                    already_terminal.append(node_id)
                child = self.core.store.session(node_id)
                if child.parent_session_id is not None and record.run_id and child.run_id == record.run_id:
                    facts = self._reconstruct_child_outcome(child)
                    if facts is not None and not self._child_fact_exists(child.parent_session_id, node_id, record.run_id):
                        self._publish_child_terminal(
                            child.parent_session_id,
                            node_id,
                            run_id=record.run_id,
                            status=facts.status,
                            output=facts.output,
                            error=facts.error,
                            stop_cause=facts.stop_cause,
                            label=facts.label,
                            notify=facts.notify,
                            deliver=False,
                            budget_exhaustion=facts.budget_exhaustion,
                            output_error=facts.output_error,
                        )
            return CancelSubtreeReceipt(
                target_session_id=target_session_id,
                expected_run_id=expected_run_id,
                cancelled=tuple(cancelled),
                already_terminal=tuple(already_terminal),
                disarmed_queued=tuple(disarmed),
                claimed_reserved=tuple(claimed),
                observed_newer_run=tuple(moved),
            )
        finally:
            with self._lifecycle_lock:
                remaining = self._cancel_fences.get(target_session_id, 0) - 1
                if remaining > 0:
                    self._cancel_fences[target_session_id] = remaining
                else:
                    self._cancel_fences.pop(target_session_id, None)

    def _parent_chain(self, record: SessionRecord) -> tuple[str, ...]:
        chain: list[str] = []
        current = record.parent_session_id
        seen = {record.session_id}
        while current is not None and current not in seen:
            chain.append(current)
            seen.add(current)
            try:
                current = self.core.store.session(current).parent_session_id
            except KeyError:
                break
        return tuple(chain)

    def _claim_reserved_child(self, child_id: str, reason: str) -> bool:
        """Claim a pristine reserved child with an owner-cancel interruption; False when it already ran.

        The claim is one lock-free CAS transaction guarded on the pristine
        state (idle, no run, expected sequence), so a racing first run either
        wins the CAS (and this claim is moot) or loses it. A claimed child
        reports cancelled and its late driver refuses to start; an explicit
        follow-up afterwards is still a legal fresh run. No session lock is
        taken: an initial driver may already hold it for its provider call.
        """
        child = self.core.store.session(child_id)
        if child.status is not SessionStatus.IDLE or child.run_id is not None:
            return False
        for event in self.core.store.events(child_id):
            if event.event_type == EventType.USER_MESSAGE:
                return False
        try:
            self.core.commit_public_staged(
                child_id,
                run_id="",
                status=SessionStatus.INTERRUPTED,
                events=(
                    EventDraft(
                        EventType.RUN_INTERRUPTED,
                        "core",
                        "core",
                        {
                            "reason": reason,
                            "stop_cause": "owner_cancel",
                            "logical_turn": "",
                            # A pristine reservation has no run to report: freeze an empty
                            # eligibility set rather than offering observers a run-less fact.
                            "eligible_owners": [],
                        },
                    ),
                ),
                checkpoint_kind=CheckpointKind.TERMINAL,
                checkpoint_payload={"step": 0, "stop_cause": "owner_cancel"},
            )
        except StoreConflict:
            return False
        self.notify_child_state_changed()
        return True
