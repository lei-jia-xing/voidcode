"""Delegated-child orchestration surface over the delegation manager.

Spawn authorization capture, joins, sends, subtree cancellation, notices, and
recovery are facade methods because the SDK and host protocols call them by
name; the durable child mechanics remain in `delegation.py`."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from uuid import uuid4

from ...sdk.client import BudgetExhaustion
from ...sdk.hooks import StopCause
from ...sdk.services import CancelSubtreeReceipt, ChildOutcome, ChildSendReceipt, DescendantNode
from ..child_contracts import ChildRunStatus, ChildSpawnRequest
from ..execution import _Execution
from ..fact_delivery import BirthAuthorization
from ..storage.store import ContinuationRecord, SessionRecord, SessionStatus, StoreConflict, StoredEvent
from ._lifecycle import CoreLifecycle


class CoreChildren(CoreLifecycle):
    """Delegated-child orchestration surface over the delegation manager."""

    def _spawn_child_callback(self, request: ChildSpawnRequest) -> str:
        return self.spawn_child(
            request.parent_session_id,
            request.prompt,
            label=request.label,
            tool_allowlist=request.tool_allowlist,
            max_steps=request.max_steps,
            readonly=request.readonly,
            notify_on_completion=request.notify_on_completion,
            owner=request.owner,
            idempotency_key=request.idempotency_key,
            root_action_id=request.root_action_id,
            initial_root_budget=request.initial_root_budget,
            admission_guard=request.admission_guard,
            result_schema=request.result_schema,
        )

    def birth_record(self, session_id: str) -> BirthAuthorization | None:
        """Decode one child's durable birth into its captured root authorization."""
        birth = self._delegation._birth_record(session_id)
        if birth is None or "root_action_budget" not in birth:
            return None
        root_id = birth.get("root_id")
        budget = birth.get("root_action_budget")
        if not isinstance(root_id, str) or not root_id or "\0" in root_id:
            return None
        if budget is not None and (type(budget) is not int or budget < 0):
            return None
        return BirthAuthorization(root_id=root_id, root_action_budget=budget)

    def capture_root_authorization(self, session: SessionRecord) -> tuple[str, int | None]:
        """Capture the current invocation's root, or inherit an immutable child birth."""
        if session.parent_session_id is not None:
            birth = self._delegation._birth_record(session.session_id)
            if birth is None or "root_action_budget" not in birth:
                raise StoreConflict(f"child session {session.session_id!r} has no captured root authorization")
            root_id = birth.get("root_id")
            root_budget = birth.get("root_action_budget")
        elif session.status in (SessionStatus.RUNNING, SessionStatus.WAITING):
            checkpoint = self.store.checkpoint(session.session_id)
            payload = checkpoint.payload if checkpoint is not None and isinstance(checkpoint.payload, Mapping) else {}
            if "initial_root_budget" not in payload:
                raise StoreConflict(f"active session {session.session_id!r} has no captured root budget")
            root_id = payload.get("root_id")
            root_budget = payload.get("initial_root_budget")
        else:
            return uuid4().hex, None
        if not isinstance(root_id, str) or not root_id or "\0" in root_id:
            raise StoreConflict(f"session {session.session_id!r} has no valid captured root action identity")
        if root_budget is not None and (type(root_budget) is not int or root_budget < 0):
            raise StoreConflict(f"session {session.session_id!r} has an invalid captured root budget")
        return root_id, root_budget

    def spawn_child(
        self,
        parent_session_id: str,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        owner: str = "",
        idempotency_key: str | None = None,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
        admission_guard: Callable[[], bool] | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        if root_action_id is None:
            if initial_root_budget is not None:
                raise ValueError("a finite child budget requires its captured root action identity")
            if idempotency_key is not None:
                retry_id = self._delegation._keyed_child_id(parent_session_id, owner, idempotency_key)
                retry_birth = self._delegation._birth_record(retry_id)
                if retry_birth is not None:
                    if "root_action_budget" not in retry_birth:
                        raise StoreConflict(f"keyed child {retry_id!r} has no captured root budget")
                    candidate_root_id = retry_birth.get("root_id")
                    if not isinstance(candidate_root_id, str) or not candidate_root_id:
                        raise StoreConflict(f"keyed child {retry_id!r} has no captured root action identity")
                    root_action_id = candidate_root_id
                    candidate_budget = retry_birth.get("root_action_budget")
                    if candidate_budget is None:
                        initial_root_budget = None
                    elif type(candidate_budget) is int and candidate_budget >= 0:
                        initial_root_budget = candidate_budget
                    else:
                        raise StoreConflict(f"keyed child {retry_id!r} has an invalid captured root budget")
            if root_action_id is None:
                parent_sess = self.store.session(parent_session_id)
                if (
                    idempotency_key is not None
                    and parent_sess.parent_session_id is None
                    and parent_sess.status
                    not in (
                        SessionStatus.RUNNING,
                        SessionStatus.WAITING,
                    )
                ):
                    retry_id = self._delegation._keyed_child_id(parent_session_id, owner, idempotency_key)
                    root_action_id = hashlib.sha256(f"root:{retry_id}".encode()).hexdigest()
                    initial_root_budget = None
                else:
                    root_action_id, initial_root_budget = self.capture_root_authorization(parent_sess)
        return self._delegation.spawn_child(
            parent_session_id,
            prompt,
            label=label,
            tool_allowlist=tool_allowlist,
            max_steps=max_steps,
            readonly=readonly,
            notify_on_completion=notify_on_completion,
            owner=owner,
            idempotency_key=idempotency_key,
            root_action_id=root_action_id,
            initial_root_budget=initial_root_budget,
            admission_guard=admission_guard,
            result_schema=result_schema,
        )

    def find_keyed_child(self, parent_session_id: str, owner: str, idempotency_key: str) -> str | None:
        """The child one parent spawned under one owner key; a read-only mapping lookup."""
        return self._delegation.find_keyed_child(parent_session_id, owner, idempotency_key)

    def join_child(self, parent_session_id: str, child_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        return self._delegation.join_child(parent_session_id, child_id, timeout_seconds=timeout_seconds)

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
        return self._delegation.send_child(
            parent_session_id,
            child_id,
            prompt,
            label=label,
            notify_on_completion=notify_on_completion,
            admission_guard=admission_guard,
        )

    def cancel_child(self, parent_session_id: str, child_id: str, reason: str = "owner cancellation") -> bool:
        return self._delegation.cancel_child(parent_session_id, child_id, reason=reason)

    def list_descendants(self, session_id: str) -> tuple[DescendantNode, ...]:
        return self._delegation.list_descendants(session_id)

    def cancel_descendant_subtree(
        self, bound_session_id: str, target_session_id: str, *, expected_run_id: str | None, reason: str = "owner cancellation"
    ) -> CancelSubtreeReceipt:
        return self._delegation.cancel_descendant_subtree(bound_session_id, target_session_id, expected_run_id=expected_run_id, reason=reason)

    def child_captured_root_id(self, child_session_id: str) -> str:
        return self._delegation._captured_child_root_id(child_session_id)

    def captured_child_root_id(self, session_id: str) -> str:
        return self._delegation._captured_child_root_id(session_id)

    def raw_birth_record(self, session_id: str) -> Mapping[str, object] | None:
        return self._delegation._birth_record(session_id)

    def recover_unstarted_child_drivers(self) -> list[tuple[str, str]]:
        return self._delegation._recover_unstarted_child_drivers()

    def recover_child_publications(self) -> list[tuple[str, str]]:
        return self._delegation._recover_child_publications()

    def first_child_message(self, session_id: str) -> ContinuationRecord | None:
        return self._delegation._first_child_message(session_id)

    def merge_child_messages(self, execution: _Execution) -> None:
        self._delegation._merge_child_messages(execution)

    def chain_of(self, session_id: str) -> frozenset[str]:
        return self._delegation._chain_of(session_id)

    def notify_requested(self, child_session: SessionRecord, events: Sequence[StoredEvent]) -> bool:
        return self._delegation._notify_requested(child_session, events)

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
    ) -> None:
        self._delegation._publish_child_terminal(
            parent_session_id,
            child_session_id,
            run_id=run_id,
            status=status,
            output=output,
            error=error,
            stop_cause=stop_cause,
            label=label,
            notify=notify,
            deliver=deliver,
            budget_exhaustion=budget_exhaustion,
        )

    def deliver_child_notice(self, parent_session_id: str, notice_id: str) -> None:
        self._delegation._deliver_child_notice(parent_session_id, notice_id)

    def notify_child_state_changed(self) -> None:
        self._delegation.notify_child_state_changed()
