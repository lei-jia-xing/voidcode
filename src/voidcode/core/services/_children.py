"""Child-session services: spawn, message, wait, join, and subtree control."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from voidcode.sdk.services import (
    CancelSubtreeReceipt,
    ChildOutcome,
    ChildSendReceipt,
    ChildSessionSnapshot,
    DescendantNode,
)

from ..child_contracts import ChildSpawnRequest
from ..events import CHILD_MESSAGE_OWNER, CHILD_OUTCOME_RECEIPT_OWNER
from ._lease import ServiceLease


class ChildServices(ServiceLease):
    """The caller's scoped child-session orchestration surface."""

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        idempotency_key: str | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        lease = self._active_lease()
        self._check_hook("spawn_child")
        # A read-only owner may delegate read-only work (reviews under plan mode):
        # the child inherits the restrictive stance, so no privilege is created.
        if lease.identity.readonly and not readonly:
            raise PermissionError("a read-only lease may only spawn child work that stays read-only")
        # Plugin-supplied arguments are validated once here, the ingress boundary.
        if tool_allowlist is not None:
            if not isinstance(tool_allowlist, (list, tuple)) or not all(isinstance(item, str) for item in tool_allowlist):
                raise ValueError("tool_allowlist must be a list of tool names or null")
            tool_allowlist = tuple(tool_allowlist)
        if max_steps is not None and type(max_steps) is not int:
            raise ValueError("max_steps must be an integer or null")
        if result_schema is not None and not isinstance(result_schema, Mapping):
            raise ValueError("result_schema must be an object or null")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("idempotency_key must be a non-empty string or null")
        root_action_id = lease.identity.root_action_id
        if root_action_id is None:
            raise ValueError("child spawning requires a captured root action identity")
        callback = self._runtime.spawn_child_callback
        if callback is None:
            raise RuntimeError("child spawning is unavailable")
        return callback(
            ChildSpawnRequest(
                parent_session_id=lease.identity.session_id,
                prompt=prompt,
                label=label,
                tool_allowlist=tool_allowlist,
                max_steps=max_steps,
                readonly=readonly or lease.identity.readonly,
                notify_on_completion=notify_on_completion,
                owner=lease.identity.owner,
                idempotency_key=idempotency_key,
                root_action_id=root_action_id,
                initial_root_budget=lease.identity.initial_root_budget,
                admission_guard=self._runtime.admission_guard(lease),
                result_schema=result_schema,
            )
        )

    def find_child(self, *, idempotency_key: str) -> str | None:
        lease = self._active_lease()
        self._check_hook("find_child")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        callback = self._runtime.find_child_callback
        if callback is None:
            raise RuntimeError("child lookup is unavailable")
        found = callback(lease.identity.session_id, lease.identity.owner, idempotency_key)
        return None if found is None else str(found)

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        lease = self._active_lease()
        self._check_hook("send_child")
        if not isinstance(child_session_id, str) or not child_session_id:
            raise ValueError("child_session_id must be a non-empty string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("child message prompt must be a non-empty string")
        # A parent that is now read-only may not steer a child that is still writable:
        # the child may have been spawned before plan mode/read-only intent existed.
        if lease.identity.readonly:
            store = self._runtime.require_store()
            child_record = store.session(child_session_id)
            if child_record.parent_session_id != lease.identity.session_id:
                raise PermissionError("child session does not belong to this parent session")
            if "read-only" not in store.session_overlays(child_session_id):
                raise PermissionError("a read-only parent cannot steer a child that is not durably read-only")
        callback = self._runtime.send_child_callback
        if callback is None:
            raise RuntimeError("child messaging is unavailable")
        return callback(
            lease.identity.session_id,
            child_session_id,
            prompt,
            label,
            notify_on_completion,
            self._runtime.admission_guard(lease),
        )

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        from ..storage.store import ObserverReceiptRecord, StagedCancellation

        lease = self._active_lease()
        self._check_hook("acknowledge_child_completion")
        if not isinstance(child_session_id, str) or not child_session_id:
            raise ValueError("child_session_id must be a non-empty string")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        store = self._runtime.require_store()
        child_record = store.session(child_session_id)
        if child_record.parent_session_id != lease.identity.session_id:
            raise PermissionError("child session does not belong to this parent session")
        callback = self._runtime.acknowledge_child_completion_callback
        if callback is None:
            raise RuntimeError("child completion acknowledgment is unavailable")
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot acknowledge child completions from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
        receipt = callback(lease.identity.owner, lease.identity.session_id, child_session_id, run_id)
        if receipt is None:
            return
        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.op_counter += 1
            lease.staging.observer_receipts.append(
                ObserverReceiptRecord(
                    session_id=receipt.source_session_id,
                    event_sequence=receipt.event_sequence,
                    owner=CHILD_OUTCOME_RECEIPT_OWNER,
                )
            )
            if receipt.notice_id is not None:
                lease.staging.cancellations.append(
                    StagedCancellation(
                        owner=CHILD_MESSAGE_OWNER,
                        continuation_id=receipt.notice_id,
                        ordinal=lease.staging.op_counter,
                        only_if_unstarted=True,
                    )
                )

    def wait_children(self, child_session_ids: Sequence[str]) -> tuple[ChildOutcome, ...]:
        from ..storage.store import ObserverReceiptRecord, StagedCancellation

        lease = self._active_lease()
        self._check_hook("wait_children")
        if lease.identity.internal_restricted:
            raise PermissionError("Cannot consume child outcomes from a read-only service lease")
        if isinstance(child_session_ids, (str, bytes)) or not isinstance(child_session_ids, Sequence):
            raise ValueError("child_session_ids must be a sequence of session ids")
        child_ids: list[str] = []
        seen_children: set[str] = set()
        store = self._runtime.require_store()
        for child_session_id in child_session_ids:
            if not isinstance(child_session_id, str) or not child_session_id:
                raise ValueError("child_session_ids must contain non-empty strings")
            if child_session_id in seen_children:
                continue
            child = store.session(child_session_id)
            if child.parent_session_id != lease.identity.session_id:
                raise PermissionError("child session does not belong to this parent session")
            seen_children.add(child_session_id)
            child_ids.append(child_session_id)
        callback = self._runtime.wait_children_callback
        if callback is None:
            raise RuntimeError("child outcome waiting is unavailable")
        deliveries = callback(lease.identity.owner, lease.identity.session_id, tuple(child_ids), self._runtime.admission_guard(lease))

        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            for delivery in deliveries:
                lease.staging.op_counter += 1
                lease.staging.observer_receipts.append(
                    ObserverReceiptRecord(
                        session_id=delivery.source_session_id,
                        event_sequence=delivery.event_sequence,
                        owner=CHILD_OUTCOME_RECEIPT_OWNER,
                        expected_run_id=delivery.outcome.run_id if delivery.source_session_id != lease.identity.session_id else None,
                        require_interruption_source=delivery.source_session_id != lease.identity.session_id,
                    )
                )
                if delivery.notice_id is not None:
                    lease.staging.cancellations.append(
                        StagedCancellation(
                            owner=CHILD_MESSAGE_OWNER,
                            continuation_id=delivery.notice_id,
                            ordinal=lease.staging.op_counter,
                            only_if_unstarted=True,
                        )
                    )
        return tuple(delivery.outcome for delivery in deliveries)

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        lease = self._active_lease()
        self._check_hook("join_child")
        callback = self._runtime.join_child_callback
        if callback is None:
            raise RuntimeError("child joining is unavailable")
        timeout = float(timeout_seconds) if isinstance(timeout_seconds, (int, float)) and not isinstance(timeout_seconds, bool) else 300.0
        return callback(lease.identity.session_id, str(child_session_id), timeout)

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        lease = self._active_lease()
        self._check_hook("cancel_child")
        callback = self._runtime.cancel_child_callback
        if callback is None:
            raise RuntimeError("child cancellation is unavailable")
        return callback(lease.identity.session_id, str(child_session_id), str(reason))

    def list_descendants(self) -> tuple[DescendantNode, ...]:
        lease = self._active_lease()
        callback = self._runtime.list_descendants_callback
        if callback is None:
            raise RuntimeError("descendant listing is unavailable")
        rows = callback(lease.identity.session_id)
        return rows

    def cancel_descendant_subtree(
        self,
        target_session_id: str,
        *,
        expected_run_id: str | None,
        reason: str = "owner cancellation",
    ) -> CancelSubtreeReceipt:
        lease = self._active_lease()
        self._check_hook("cancel_descendant_subtree")
        if not isinstance(target_session_id, str) or not target_session_id:
            raise ValueError("target_session_id must be a non-empty string")
        if expected_run_id is not None and (not isinstance(expected_run_id, str) or not expected_run_id):
            raise ValueError("expected_run_id must be a non-empty string or null")
        if lease.identity.internal_restricted:
            raise PermissionError("Cannot cancel a descendant subtree from a read-only service lease")
        callback = self._runtime.cancel_subtree_callback
        if callback is None:
            raise RuntimeError("subtree cancellation is unavailable")
        receipt = callback(lease.identity.session_id, target_session_id, expected_run_id, str(reason))
        return receipt

    def _read_child_snapshot(self, child_session_id: str) -> ChildSessionSnapshot | None:
        """Read one committed descendant snapshot through the core's caller-scoped callback."""
        lease = self._active_lease()
        if not isinstance(child_session_id, str) or not child_session_id:
            raise ValueError("child_session_id must be a non-empty string")
        callback = self._runtime.read_child_snapshot_callback
        if callback is None:
            raise RuntimeError("descendant snapshots are unavailable")
        snapshot = callback(lease.identity.session_id, child_session_id)
        if snapshot is not None and snapshot.descendant.session_id != child_session_id:
            raise RuntimeError("runtime returned a malformed child session snapshot")
        return snapshot
