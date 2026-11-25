"""Lease-staged state, events, continuations, and owner budgets."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Literal
from uuid import uuid4

from voidcode.sdk.json_values import own_json_object
from voidcode.sdk.services import PluginBudget

from ..storage.store import StateVersionConflict
from ._lease import ServiceLease


class StateServices(ServiceLease):
    """State, event, continuation, and budget staging on the current lease."""

    def _scope_key(self, scope: str) -> str:
        if scope not in ("session", "workspace"):
            raise ValueError(f"Invalid scope: {scope!r}")
        lease = self._active_lease()
        return lease.identity.session_id if scope == "session" else str(lease.identity.workspace)

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        lease = self._active_lease()
        key = self._scope_key(scope)
        staged = lease.staging.state.get((scope, key))
        if staged is not None:
            st, ver, _baseline = staged
            return (own_json_object(st) if isinstance(st, Mapping) else None, ver)
        st, ver = self._runtime.require_store().plugin_state(lease.identity.owner, scope, key)
        return (own_json_object(st) if isinstance(st, Mapping) else None, ver)

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        lease = self._active_lease()
        self._check_hook("replace_state")
        key = self._scope_key(scope)
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot modify state from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            owned = own_json_object(state)
            if type(expected_version) is not int:
                raise ValueError("expected_version must be an integer")
            staged = lease.staging.state.get((scope, key))
            if staged is not None:
                # Exactly one future version per lease: the first write published
                # it, later writes in the same lease must target that version.
                _st, local_version, baseline = staged
                if expected_version != local_version:
                    raise StateVersionConflict(f"state CAS conflict within lease: expected version {local_version}, got {expected_version}")
            else:
                _, current = self._runtime.require_store().plugin_state(lease.identity.owner, scope, key)
                if expected_version != current:
                    raise StateVersionConflict(f"state CAS conflict: expected version {expected_version}, got {current}")
                baseline = current
            local_version = baseline + 1
            lease.staging.state[(scope, key)] = (owned, local_version, baseline)
        return local_version

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        from ..storage.store import EventDraft

        lease = self._active_lease()
        self._check_hook("stage_event")
        owned = own_json_object(payload)
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot stage events from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.events.append(
                EventDraft(
                    event_type=f"plugin.{lease.identity.owner}.{event_type}",
                    source=f"plugin.{lease.identity.owner}",
                    actor="plugin",
                    payload=owned,
                )
            )

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        from ..storage.store import MAX_CONTINUATION_PAYLOAD_BYTES, StagedContinuationDraft, canonical_json

        lease = self._active_lease()
        identity = lease.identity
        self._check_hook("enqueue_continuation")
        owned = own_json_object(payload)
        if role not in ("system", "user"):
            raise ValueError("role must be 'system' or 'user'")
        if lease.identity.hook:
            raise PermissionError("post-tool hooks cannot start nested governed work")
        key = str(idempotency_key) if idempotency_key is not None else None
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot enqueue continuations from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            existing = next(
                (c.continuation_id for c in lease.staging.continuations if c.idempotency_key == key and key is not None),
                None,
            )
            if existing is None and key is not None:
                existing = self._runtime.require_store().continuation_id_for_idempotency(lease.identity.session_id, lease.identity.owner, key)
            if existing is not None:
                return existing
            # After the dedupe return: a keyed retry names its existing row without staging anything new.
            continuation_size = len(canonical_json(owned).encode("utf-8"))
            if continuation_size > MAX_CONTINUATION_PAYLOAD_BYTES:
                raise ValueError(
                    f"continuation payload is {continuation_size} bytes, "
                    f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
                )
            # A keyed identity is derived from the key itself, so two
            # concurrent leases that both stage before either commits
            # return the SAME id: the store's key dedupe then keeps
            # exactly one durable row that every caller's id names.
            cid = uuid4().hex if key is None else hashlib.sha256("\0".join((identity.session_id, identity.owner, key)).encode("utf-8")).hexdigest()
            lease.staging.op_counter += 1
            lease.staging.continuations.append(
                StagedContinuationDraft(
                    continuation_id=cid,
                    owner=lease.identity.owner,
                    source=f"plugin.{lease.identity.owner}",
                    payload=owned,
                    idempotency_key=key,
                    ordinal=lease.staging.op_counter,
                    root_id=lease.identity.root_action_id,
                    role=role,
                )
            )
        return cid

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        from ..storage.store import StagedCancellation

        lease = self._active_lease()
        self._check_hook("cancel_continuations")
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot cancel continuations from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.op_counter += 1
            lease.staging.cancellations.append(
                StagedCancellation(
                    owner=lease.identity.owner,
                    continuation_id=str(continuation_id) if continuation_id is not None else None,
                    ordinal=lease.staging.op_counter,
                )
            )

    def set_budget(
        self,
        *,
        since_sequence: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        lease = self._active_lease()
        self._check_hook("set_budget")
        if type(since_sequence) is not int:
            raise ValueError("since_sequence must be an integer")
        budget = PluginBudget(
            owner=lease.identity.owner,
            since_sequence=since_sequence,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot set budget from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.budget = budget

    def get_budget(self) -> PluginBudget | None:
        lease = self._active_lease()
        with self._runtime.lease_lock:
            staged_budget = lease.staging.budget
        if staged_budget == "clear":
            return None
        if staged_budget is not None:
            return staged_budget
        callback = self._runtime.budget_callback
        if callback is None:
            raise RuntimeError("budget service is unavailable in this runtime")
        return callback(lease.identity.session_id, lease.identity.owner)

    def clear_budget(self) -> None:
        lease = self._active_lease()
        self._check_hook("clear_budget")
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot clear budget from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.budget = "clear"
