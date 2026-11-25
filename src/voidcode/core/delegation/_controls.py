"""Owner-facing child controls: sends, cancellation, descendant listing, and snapshots."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Literal
from uuid import uuid4

from ...sdk.child_schema import CHILD_PROMPT_MAX_BYTES
from ...sdk.services import (
    ChildOutcome,
    ChildSendReceipt,
    ChildSessionSnapshot,
    DescendantNode,
)
from ..events import CHILD_MESSAGE_OWNER, EventType
from ..storage.store import (
    ContinuationStatus,
    FencedSubtree,
    SessionStatus,
    StoreConflict,
    StoredEvent,
)
from ._outcomes import _decode_terminal_run_capture
from ._wait import _DelegationWait


class _DelegationControls(_DelegationWait):
    """Owner-facing child controls: sends, cancellation, descendant listing, and snapshots."""

    def send_child(
        self,
        parent_session_id: str,
        child_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
        admission_guard: Callable[[], bool] | None = None,
    ) -> ChildSendReceipt:
        """Send one attributed follow-up to a direct child, in the child's own session.

        The message is a durable continuation, never a filesystem mutation or a user-authority
        action. A running or approval-waiting child consumes it at its next committed batch
        boundary; an open child reserves a run identity and starts. A recoverably interrupted
        child keeps the message durable but cannot claim it without an explicit resume, so the
        receipt says ``parked`` instead of promising a boundary. Scope and stance can never widen:
        a parent that is now read-only may not steer a child that is not durably read-only.
        """
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > CHILD_PROMPT_MAX_BYTES:
            raise ValueError(f"child prompt must be nonempty UTF-8 text within {CHILD_PROMPT_MAX_BYTES // 1024} KiB")
        if type(notify_on_completion) is not bool:
            raise ValueError("notify_on_completion must be a boolean")
        self.core.ensure_open()
        parent = self.core.session(parent_session_id)
        child = self._owned_child(parent_session_id, child_id)
        self.core.check_session(child)
        if self.core.session_read_only(parent.session_id) and not self.core.session_read_only(child.session_id):
            raise PermissionError("a read-only parent cannot steer a child that is not durably read-only")
        message_id = uuid4().hex
        attributed = "[Follow-up from your parent agent; delegated agent input, not a direct user message]\n\n" + prompt
        root_id = self._root_action_id_for_send(child)
        # Fast pre-check on a captured chain; the authoritative fence decision
        # happens inside the store transaction via _guard_fence.
        chain = self._chain_of(parent_session_id)
        with self._lifecycle_lock:
            fenced = self._fence_holds(chain, child_id)
            if fenced is not None:
                raise PermissionError(f"session {child_id!r} is inside subtree {fenced!r} under cancellation")
        try:
            self.core.store.enqueue_continuation(
                message_id,
                CHILD_MESSAGE_OWNER,
                child_id,
                "core",
                run_id=parent.run_id or uuid4().hex,
                revision_id=self.core.revision_id,
                payload={
                    "kind": "child_followup",
                    "text": attributed,
                    "prompt": prompt,
                    "label": label,
                    "notify_on_completion": notify_on_completion,
                },
                root_id=root_id,
                role="user",
                caller_chain=chain,
                admission_guard=admission_guard,
            )
        except FencedSubtree as exc:
            raise PermissionError(str(exc)) from exc
        outstanding = child.status in (SessionStatus.RUNNING, SessionStatus.WAITING)
        delivery: Literal["started", "queued", "parked"] = "queued"
        started_run: str | None = None
        if not outstanding:
            run_id = uuid4().hex
            try:
                claimed, _session = self.core.store.claim_and_start_continuation(
                    child_id,
                    run_id,
                    uuid4().hex,
                    revision_id=self.core.revision_id,
                    initial_root_budget=None,
                    continuation_id=message_id,
                    caller_chain=chain,
                )
                self.core.notify_commit()
            except FencedSubtree:
                # The fence won the race: the message stays queued and the
                # canceller's disarm sweep owns it. The receipt honestly says queued.
                pass
            except StoreConflict as exc:
                # A loss to a concurrent claim stays queued behind that run. An interrupted
                # child with a non-terminal checkpoint cannot claim follow-ups without an
                # explicit resume, so its receipt must not promise a future boundary.
                current = self.core.store.session(child_id)
                if current.status is SessionStatus.INTERRUPTED and not self.core.admits_fresh_work(current):
                    delivery = "parked"
                elif self.core.store.continuation_status(message_id) not in (
                    ContinuationStatus.PENDING,
                    ContinuationStatus.CLAIMED,
                ):
                    raise StoreConflict(f"follow-up {message_id!r} is no longer durably claimable") from exc
            else:
                # The receipt names the run the store actually claimed, never an unused token.
                delivery, started_run = "started", claimed.run_id
        self._driver_executor.submit(self._deliver_child_messages, child_id, message_id)
        status = "running" if delivery == "started" else self.core.store.session(child_id).status
        self.notify_child_state_changed()
        return ChildSendReceipt(
            child_session_id=child_id,
            delivery=delivery,
            child_status=status,
            run_id=started_run,
            message_id=message_id,
        )

    def cancel_child(self, parent_session_id: str, child_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel a child (executing, parked, reserved-but-unstarted, or with queued follow-ups).

        A send that overlaps this call may legally linearize after the pending sweep and start
        new work. Cancellation therefore publishes only the run identity it observed, never a
        newer run that a concurrent follow-up legitimately claimed. A reserved child whose
        first run never started is claimed with an owner-cancel interruption, so a late
        driver thread can never start it afterwards.
        """
        self._owned_child(parent_session_id, child_id)
        target_run = self.core.store.session(child_id).run_id
        if target_run is None and self._claim_reserved_child(child_id, reason):
            return True
        cancelled = self.core.cancel_session(child_id, reason, cause="owner_cancel")
        pending = self.core.store.cancel_pending_continuations(child_id, CHILD_MESSAGE_OWNER)
        if cancelled or pending > 0:
            # The sweep above commits after cancel_session's notify; wake joiners past it.
            self.notify_child_state_changed()
        child = self.core.store.session(child_id)
        if child.parent_session_id is not None and target_run and child.run_id == target_run:
            facts = self._reconstruct_child_outcome(child)
            if facts is not None and not self._child_fact_exists(parent_session_id, child_id, target_run):
                self._publish_child_terminal(
                    parent_session_id,
                    child_id,
                    run_id=target_run,
                    status=facts.status,
                    output=facts.output,
                    error=facts.error,
                    stop_cause=facts.stop_cause,
                    label=facts.label,
                    notify=facts.notify,
                    budget_exhaustion=facts.budget_exhaustion,
                    output_error=facts.output_error,
                    deliver=False,
                )
        return cancelled or pending > 0

    def list_descendants(self, session_id: str) -> tuple[DescendantNode, ...]:
        """Every transitive descendant of one session with verbatim lifecycle facts."""
        self.core.ensure_open()
        caller = self.core.session(session_id)
        timestamps = self.core.store.descendant_timestamps(session_id)
        now_ms = int(time.time() * 1000)
        nodes: list[DescendantNode] = []
        for child in self.core.store.descendant_sessions(session_id):
            if child.workspace != caller.workspace:
                continue
            label = self._child_label(child.parent_session_id or "", child.session_id) if child.parent_session_id else ""
            # latest_stop_cause(run_id) returns None for the "" bookkeeping
            # marker; a claimed-reserved node reports its committed owner_cancel.
            stop_cause = self.core.store.latest_stop_cause(child.session_id, child.run_id or None) or ("owner_cancel" if child.run_id == "" else None)
            queued = self.core.store.has_queued_child_followup(child.session_id)
            tool_fact = self.core.store.latest_tool_fact(child.session_id)
            activity = None if tool_fact is None else f"{tool_fact[0]} {tool_fact[1]}"
            created_ms, updated_ms = timestamps.get(child.session_id, (None, None))
            elapsed_ms: int | None = None
            if created_ms is not None and updated_ms is not None:
                if child.status in (SessionStatus.IDLE, SessionStatus.RUNNING, SessionStatus.WAITING):
                    elapsed_ms = max(0, now_ms - created_ms)
                else:
                    elapsed_ms = max(0, updated_ms - created_ms)
            nodes.append(
                DescendantNode(
                    session_id=child.session_id,
                    parent_session_id=child.parent_session_id,
                    depth=child.depth,
                    status=child.status.value,
                    # The reserved-claim bookkeeping marker ("") is not a real
                    # run: project it as null so detail/cancel bindings stay
                    # valid and a claimed parent with descendants stays selectable.
                    run_id=child.run_id or None,
                    stop_cause=stop_cause,
                    label=label or None,
                    activity=activity,
                    turn=child.turn,
                    last_event_sequence=child.last_event_sequence,
                    has_queued_followup=queued,
                    awaiting_approval=child.status is SessionStatus.WAITING,
                    started_at_ms=created_ms,
                    updated_at_ms=updated_ms,
                    elapsed_ms=elapsed_ms,
                )
            )
        return tuple(nodes)

    def read_child_snapshot(self, caller_session_id: str, child_session_id: str) -> ChildSessionSnapshot | None:
        """Read one caller-visible descendant and its latest eligible committed result.

        Non-visible and nonexistent ids share ``None``. Terminal child events are append-only;
        only a successful publication carrying nonempty inline output or a full output artifact
        replaces the latest result. Failed, interrupted, cancelled, empty, and preview-only
        publications do not erase a prior result. ``terminal_run`` instead projects the newest
        committed terminal fact exactly, whatever its eligibility, so its capture diagnosis is
        never attached to the prior eligible output. Inline committed output is materialized as a
        deduplicated immutable artifact for resource paging; the committed event and join result
        stay unchanged. The event contract has no separate OMP-style accepted-yield marker.
        """
        self.core.ensure_open()
        initial_descendant = next(
            (node for node in self.list_descendants(caller_session_id) if node.session_id == child_session_id),
            None,
        )
        if initial_descendant is None:
            return None

        # Use the first scoped projection for authorization and the stable parent lookup. The
        # current-run projection is refreshed after reading committed publications so a result
        # published between these reads cannot be presented as newer than the sampled run.
        parent_session_id = initial_descendant.parent_session_id

        latest_outcome: ChildOutcome | None = None
        terminal_event: StoredEvent | None = None
        if parent_session_id is not None:
            finished = self.core.store.events_by_types(parent_session_id, (EventType.CHILD_FINISHED,))
            for event in reversed(finished):
                payload = event.payload
                if event.event_type != EventType.CHILD_FINISHED or payload.get("child_session_id") != child_session_id:
                    continue
                if terminal_event is None:
                    terminal_event = event
                if latest_outcome is None:
                    output = payload.get("output")
                    output_artifact = payload.get("output_sha256")
                    accepted = payload.get("status") == "completed" and (
                        isinstance(output, str) and bool(output) or isinstance(output_artifact, str) and bool(output_artifact)
                    )
                    if accepted:
                        latest_outcome = self._child_outcome(child_session_id, payload)
                        if latest_outcome.output_sha256 is None and latest_outcome.output is not None:
                            reference = self._spill_child_output(latest_outcome.output)
                            digest = reference.get("sha256")
                            if not isinstance(digest, str) or not digest:
                                raise RuntimeError("inline child output did not produce an immutable artifact reference")
                            latest_outcome = replace(latest_outcome, output_sha256=digest)
                if terminal_event is not None and latest_outcome is not None:
                    break
        terminal_run = None if terminal_event is None else _decode_terminal_run_capture(child_session_id, terminal_event)

        descendant = next(
            (node for node in self.list_descendants(caller_session_id) if node.session_id == child_session_id),
            None,
        )
        if descendant is None:
            return None
        return ChildSessionSnapshot(descendant=descendant, latest_outcome=latest_outcome, terminal_run=terminal_run)
