"""Wakeup scheduling and staged cancellation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Literal
from uuid import uuid4

from voidcode.sdk.json_values import own_json_object

from ._lease import ServiceLease


class WakeupServices(ServiceLease):
    """Owner-scoped wakeups, either committed now or staged with the lease."""

    def schedule_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        from ..storage.store import MAX_WAKEUP_PAYLOAD_BYTES, canonical_json

        lease = self._active_lease()
        self._check_hook("schedule_wakeup")
        if lease.identity.internal_restricted:
            raise PermissionError("Cannot schedule wakeups from a read-only service lease")
        if type(due_at_ms) is not int or due_at_ms < 0:
            raise ValueError("due_at_ms must be >= 0")
        if role not in ("system", "user"):
            raise ValueError("role must be 'system' or 'user'")
        key = str(idempotency_key) if isinstance(idempotency_key, str) and idempotency_key else None
        owned = own_json_object(payload)
        wakeup_size = len(canonical_json(owned).encode("utf-8"))
        if wakeup_size > MAX_WAKEUP_PAYLOAD_BYTES:
            raise ValueError(f"wakeup payload is {wakeup_size} bytes, exceeding the {MAX_WAKEUP_PAYLOAD_BYTES}-byte wakeup-payload bound")
        if key is None:
            wakeup_id = uuid4().hex
        else:
            wakeup_id = hashlib.sha256("\0".join((lease.identity.session_id, lease.identity.owner, "wakeup", key)).encode("utf-8")).hexdigest()
        if not isinstance(lease.identity.root_action_id, str) or not lease.identity.root_action_id:
            raise RuntimeError("wakeup scheduling requires a captured root action identity")
        self._runtime.require_store().schedule_wakeup(
            wakeup_id,
            lease.identity.owner,
            str(lease.identity.workspace),
            lease.identity.session_id,
            owned,
            due_at_ms,
            str(role),
            root_action_id=lease.identity.root_action_id,
            root_action_budget=lease.identity.initial_root_budget,
        )
        return wakeup_id

    def cancel_wakeup(self, wakeup_id: str) -> bool:
        lease = self._active_lease()
        self._check_hook("cancel_wakeup")
        if lease.identity.internal_restricted:
            raise PermissionError("Cannot cancel wakeups from a read-only service lease")
        return self._runtime.require_store().cancel_wakeup(str(wakeup_id), lease.identity.owner)

    def stage_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Stage a wakeup whose row commits atomically with this callback's state/events."""
        from ..storage.store import MAX_WAKEUP_PAYLOAD_BYTES, StagedWakeup, StoreConflict, canonical_json

        lease = self._active_lease()
        self._check_hook("stage_wakeup")
        if lease.identity.internal_restricted:
            raise PermissionError("Cannot schedule wakeups from a read-only service lease")
        if type(due_at_ms) is not int or due_at_ms < 0:
            raise ValueError("due_at_ms must be >= 0")
        if role not in ("system", "user"):
            raise ValueError("role must be 'system' or 'user'")
        key = str(idempotency_key) if isinstance(idempotency_key, str) and idempotency_key else None
        owned = own_json_object(payload)
        wakeup_size = len(canonical_json(owned).encode("utf-8"))
        if wakeup_size > MAX_WAKEUP_PAYLOAD_BYTES:
            raise ValueError(f"wakeup payload is {wakeup_size} bytes, exceeding the {MAX_WAKEUP_PAYLOAD_BYTES}-byte wakeup-payload bound")
        if not isinstance(lease.identity.root_action_id, str) or not lease.identity.root_action_id:
            raise RuntimeError("wakeup scheduling requires a captured root action identity")
        wakeup_id = (
            uuid4().hex
            if key is None
            else hashlib.sha256("\0".join((lease.identity.session_id, lease.identity.owner, "wakeup", key)).encode("utf-8")).hexdigest()
        )
        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            if any(item.wakeup_id == wakeup_id for item in lease.staging.wakeups):
                return wakeup_id
            status = self._runtime.require_store().wakeup_status(wakeup_id)
            if status is not None:
                # A keyed retry names its already-committed pending/claimed row. A terminal
                # row (cancelled/completed/failed) must fail loudly instead of naming a
                # dead identity that the commit's ON CONFLICT would silently reuse.
                if status in ("pending", "claimed"):
                    return wakeup_id
                raise StoreConflict(f"keyed wakeup {wakeup_id!r} already exists with terminal status {status!r}")
            lease.staging.wakeups.append(
                StagedWakeup(
                    wakeup_id=wakeup_id,
                    owner=lease.identity.owner,
                    session_id=lease.identity.session_id,
                    payload=owned,
                    due_at=due_at_ms,
                    role=role,
                )
            )
        return wakeup_id

    def stage_cancel_wakeup(self, wakeup_id: str) -> None:
        """Stage cancellation of a pending wakeup owned by this plugin for the same atomic commit."""
        from ..storage.store import StagedWakeupCancel

        lease = self._active_lease()
        self._check_hook("stage_cancel_wakeup")
        if lease.identity.internal_restricted:
            raise PermissionError("Cannot cancel wakeups from a read-only service lease")
        if not isinstance(wakeup_id, str) or not wakeup_id:
            raise ValueError("wakeup_id must be a non-empty string")
        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staging.wakeup_cancels.append(StagedWakeupCancel(wakeup_id=wakeup_id, owner=lease.identity.owner))
