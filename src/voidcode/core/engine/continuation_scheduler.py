"""Continuation lifecycle and scheduling domain.

Houses:
- Claiming and executing pending continuations as governed runs
- Reconciling recovering claimed continuations across process boundaries
- Root action budget enforcement for continuations
- Completing continuation statuses and delivering child completion notices
- Settling continuation cancellations targeting claimed durable work
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from ...sdk.hooks import StopCause
from ...sdk.json_values import json_wire_object
from ...sdk.services import BudgetExhaustion, ContinuationResult
from ..cancellation import Cancellation
from ..child_contracts import ChildRunStatus
from ..delegation import _child_run_status, _terminal_run_result
from ..events import CHILD_MESSAGE_OWNER, child_completion_notice_id
from ..execution import _Cursor, _Execution
from ..storage.store import (
    CheckpointKind,
    ContinuationStatus,
    SessionRecord,
    SessionStatus,
    SqliteStore,
    StagedCancellation,
    StoreConflict,
)
from .governance import validate_steps
from .tool_execution import cancels_claimed

if TYPE_CHECKING:
    from .turn_execution import CoreResult


@dataclass(frozen=True, slots=True)
class ContinuationDrainSpec:
    """Parameters governing a continuation drain sequence."""

    session_id: str
    limit: int = 10
    max_steps: int | None = None
    continuation_id: str | None = None
    automatic: bool = False


class ContinuationSchedulerHost(Protocol):
    """Host requirements for claiming, scheduling, and running continuations."""

    @property
    def revision_id(self) -> str: ...

    @property
    def store(self) -> SqliteStore: ...

    def session_lock(self, session_id: str) -> AbstractContextManager[None]: ...

    def ensure_open(self) -> None: ...

    def reset_abort(self) -> None: ...

    def session(self, session_id: str) -> SessionRecord: ...

    def active_abort(self, session_id: str) -> Cancellation | None: ...

    def register_run(self, session_id: str, run_id: str | None, abort: Cancellation) -> None: ...

    def unregister_run(self, session_id: str) -> None: ...

    def notify_commit(self) -> None: ...

    def chain_of(self, session_id: str) -> frozenset[str]: ...

    def stop(
        self,
        execution: _Execution,
        reason: str,
        *,
        stop_cause: StopCause,
        budget_exhaustion: BudgetExhaustion | None = None,
    ) -> CoreResult: ...

    def drive(self, execution: _Execution) -> CoreResult: ...

    def publish_child_terminal(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        run_id: str,
        status: ChildRunStatus,
        output: str | None,
        error: str | None,
        stop_cause: StopCause | None,
        label: str,
        notify: bool,
        deliver: bool,
        budget_exhaustion: BudgetExhaustion | None,
    ) -> None: ...

    def deliver_child_notice(self, parent_session_id: str, notice_id: str) -> None: ...

    def notify_child_state_changed(self) -> None: ...

    def settle_parked_cancel(self, session_id: str, reason: str, stop_cause: StopCause) -> None: ...


class ContinuationScheduler:
    """Manages claiming, executing, recovering, and settling continuations."""

    def __init__(self, host: ContinuationSchedulerHost) -> None:
        self._host = host

    def drain(
        self,
        session_id: str,
        *,
        limit: int = 10,
        max_steps: int | None = None,
        continuation_id: str | None = None,
        automatic: bool = False,
    ) -> tuple[ContinuationResult, ...]:
        """Claim and execute pending continuations as governed runs with plugin provenance.

        `continuation_id` targets one exact durable message (used by startup recovery); a plain
        drain keeps the existing oldest-first behavior.
        Automatic delivery leaves completion notices pending on interrupted sessions;
        an explicit follow-up or drain is still new intent.
        """
        validate_steps(max_steps)
        results: list[ContinuationResult] = []
        parent_deliveries: list[tuple[str, str]] = []
        with self._host.session_lock(session_id):
            self._host.ensure_open()
            self._host.reset_abort()
            s = self._host.session(session_id)
            abort = Cancellation()
            self._host.register_run(session_id, None, abort)

            try:
                for _ in range(limit):
                    run_id = uuid4().hex
                    logical_turn = uuid4().hex
                    claimed = self._host.store.claimed_continuations(session_id, limit=100)
                    if continuation_id is not None:
                        claimed = tuple(record for record in claimed if record.continuation_id == continuation_id)
                    recovering = bool(claimed)
                    if recovering:
                        cont_record = claimed[0]
                        run_id = cont_record.run_id
                    else:
                        if s.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
                            break
                        pending = self._host.store.pending_continuations(session_id, limit=100)
                        if continuation_id is not None:
                            pending = tuple(record for record in pending if record.continuation_id == continuation_id)
                        if pending and (not isinstance(pending[0].root_id, str) or not pending[0].root_id):
                            missing = pending[0]
                            self._host.store.complete_continuation(missing.continuation_id, status=ContinuationStatus.FAILED)
                            results.append(
                                ContinuationResult(
                                    continuation_id=missing.continuation_id,
                                    owner=missing.owner,
                                    session_id=session_id,
                                    status="failed",
                                    payload=missing.payload,
                                    run_status="failed",
                                    run_id=missing.run_id,
                                    error="queued continuation has no captured root action identity; refusing to create a new budget",
                                    stop_cause="error",
                                )
                            )
                            if continuation_id is not None:
                                break
                            continue
                        try:
                            cont_record, session_record = self._host.store.claim_and_start_continuation(
                                session_id,
                                run_id,
                                logical_turn,
                                revision_id=self._host.revision_id,
                                initial_root_budget=None,
                                max_steps=max_steps,
                                continuation_id=continuation_id,
                                caller_chain=self._host.chain_of(session_id),
                                automatic=automatic,
                            )
                            self._host.notify_commit()
                        except StoreConflict:
                            break

                    if not isinstance(cont_record.root_id, str) or not cont_record.root_id:
                        self._host.store.complete_continuation(cont_record.continuation_id, status=ContinuationStatus.FAILED)
                        results.append(
                            ContinuationResult(
                                continuation_id=cont_record.continuation_id,
                                owner=cont_record.owner,
                                session_id=session_id,
                                status="failed",
                                payload=cont_record.payload,
                                run_status="failed",
                                run_id=run_id,
                                error="continuation has no captured root action identity; refusing to create a new budget",
                                stop_cause="error",
                            )
                        )
                        break

                    if recovering:
                        checkpoint = self._host.store.checkpoint(session_id)
                        if checkpoint is None or checkpoint.kind is CheckpointKind.TERMINAL:
                            self._host.store.complete_continuation(cont_record.continuation_id, status=ContinuationStatus.FAILED)
                            results.append(
                                ContinuationResult(
                                    continuation_id=cont_record.continuation_id,
                                    owner=cont_record.owner,
                                    session_id=session_id,
                                    status="failed",
                                    payload=cont_record.payload,
                                    run_status="failed",
                                    run_id=run_id,
                                    error="claimed continuation has no resumable checkpoint",
                                    stop_cause="error",
                                )
                            )
                            break
                        cp = checkpoint.payload if isinstance(checkpoint.payload, Mapping) else {}
                        cursor = _Cursor.model_validate(json_wire_object(cp))
                        if (
                            str(checkpoint.run_id) != str(cont_record.run_id)
                            or str(cp.get("continuation_id")) != cont_record.continuation_id
                            or cursor.root_id != cont_record.root_id
                        ):
                            self._host.store.complete_continuation(cont_record.continuation_id, status=ContinuationStatus.FAILED)
                            results.append(
                                ContinuationResult(
                                    continuation_id=cont_record.continuation_id,
                                    owner=cont_record.owner,
                                    session_id=session_id,
                                    status="failed",
                                    payload=cont_record.payload,
                                    run_status="failed",
                                    run_id=cont_record.run_id,
                                    error="claimed continuation does not own its checkpoint or captured root action identity",
                                    stop_cause="error",
                                )
                            )
                            break
                        run_id = cont_record.run_id
                        session_record = self._host.store.session(session_id)
                    else:
                        cur_session = self._host.store.session(session_id)
                        root_id = cont_record.root_id
                        root_state = self._host.store.root_budget_state(root_id)
                        payload_max_steps = cont_record.payload.get("max_steps")
                        cursor = _Cursor(
                            continuation_id=cont_record.continuation_id,
                            root_id=root_id,
                            logical_turn=logical_turn,
                            initial_root_budget=None if root_state is None else root_state[0],
                            max_steps=payload_max_steps if type(payload_max_steps) is int else None,
                        )
                        session_record = cur_session
                    execution = _Execution(session_record, cursor, list(self._host.store.events(session_id)), run_id)
                    root_state = self._host.store.root_budget_state(cursor.root_id)
                    if max_steps is not None:
                        cursor.max_steps = max_steps if cursor.max_steps is None else min(cursor.max_steps, max_steps)
                    self._host.register_run(session_id, run_id, abort)
                    if root_state is not None and root_state[1] >= root_state[0]:
                        exhaustion = BudgetExhaustion(
                            dimension="root_actions",
                            source="root_action_budget",
                            limit=root_state[0],
                            consumed=root_state[1],
                            remaining=max(0, root_state[0] - root_state[1]),
                            continue_with="extend_root_actions",
                        )
                        run_res = self._host.stop(
                            execution,
                            f"root action budget exhausted: consumed {root_state[1]} of {root_state[0]}, remaining 0",
                            stop_cause="limit_reached",
                            budget_exhaustion=exhaustion,
                        )
                    else:
                        run_res = self._host.drive(execution)

                    status_str = ContinuationStatus.COMPLETED
                    if run_res.status in ("completed", "idle"):
                        status_str = ContinuationStatus.COMPLETED
                    elif run_res.status == "failed":
                        status_str = ContinuationStatus.FAILED
                    elif run_res.status == "waiting":
                        status_str = ContinuationStatus.CLAIMED
                    elif run_res.status == "interrupted":
                        if run_res.stop_cause in ("user_stop", "owner_cancel"):
                            status_str = ContinuationStatus.CANCELLED
                        else:
                            status_str = ContinuationStatus.CLAIMED

                    if status_str is not ContinuationStatus.CLAIMED:
                        self._host.store.complete_continuation(cont_record.continuation_id, status=status_str)

                    results.append(
                        ContinuationResult(
                            continuation_id=cont_record.continuation_id,
                            owner=cont_record.owner,
                            session_id=session_id,
                            status=status_str.value,
                            payload=cont_record.payload,
                            run_status=run_res.status,
                            run_id=run_res.run_id,
                            output=run_res.output,
                            error=run_res.error,
                            stop_cause=run_res.stop_cause,
                            budget_exhaustion=run_res.budget_exhaustion,
                        )
                    )
                    if cont_record.owner == CHILD_MESSAGE_OWNER and cont_record.payload.get("kind") == "child_followup":
                        child = self._host.store.session(session_id)
                        parent_id = child.parent_session_id
                        notify = bool(cont_record.payload.get("notify_on_completion", True))
                        if parent_id is not None and _terminal_run_result(run_res):
                            self._host.publish_child_terminal(
                                parent_id,
                                session_id,
                                run_id=run_res.run_id,
                                status=_child_run_status(run_res),
                                output=run_res.output,
                                error=run_res.error,
                                stop_cause=run_res.stop_cause,
                                label=str(cont_record.payload.get("label", "")),
                                notify=notify,
                                deliver=False,
                                budget_exhaustion=run_res.budget_exhaustion,
                            )
                            if notify:
                                parent_deliveries.append((parent_id, child_completion_notice_id(parent_id, session_id, run_res.run_id)))
                    if run_res.status in ("interrupted", "failed", "waiting"):
                        break
            finally:
                self._host.unregister_run(session_id)
                # A parked follow-up run commits no terminal fact; wake child joiners past the state change.
                if self._host.store.session(session_id).parent_session_id is not None:
                    self._host.notify_child_state_changed()

        for parent_id, notice_id in dict.fromkeys(parent_deliveries):
            self._host.deliver_child_notice(parent_id, notice_id)
        return tuple(results)

    def settle_cancellations(
        self,
        session_id: str,
        cancellations: Sequence[StagedCancellation],
        *,
        reason: str = "continuation was cancelled by owner",
    ) -> None:
        """Settle work whose own claimed continuation an owner just cancelled."""
        if not cancellations:
            return
        checkpoint = self._host.store.checkpoint(session_id)
        cursor = _Cursor()
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            cursor = _Cursor.model_validate(json_wire_object(checkpoint.payload))
        if cursor.continuation_id is None:
            return
        claim_owner = self._host.store.continuation_owner(cursor.continuation_id)
        if not any(cancels_claimed(cancellation, cursor.continuation_id, claim_owner) for cancellation in cancellations):
            return
        active = self._host.active_abort(session_id)
        if active is not None and not active.cancelled:
            active.set_cancelled(True, reason=reason, stop_cause="owner_cancel")
            return
        self._host.settle_parked_cancel(session_id, reason, "owner_cancel")
