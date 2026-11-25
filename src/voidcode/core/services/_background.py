"""Background-action services: staged enqueue, cancellation, and status."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from uuid import uuid4

from voidcode.sdk.json_values import own_json_object
from voidcode.sdk.services import BackgroundActionStatus

from ._lease import ServiceLease


class BackgroundServices(ServiceLease):
    """Owner-scoped background action adapters over the core's registry."""

    def enqueue_background_action(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        from ..storage.store import BACKGROUND_ACTION_SOURCE, MAX_CONTINUATION_PAYLOAD_BYTES, StagedContinuationDraft, canonical_json

        lease = self._active_lease()
        identity = lease.identity
        self._check_hook("enqueue_background_action")
        owned = own_json_object(payload)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("idempotency_key must be a non-empty string or null")
        key = str(idempotency_key) if idempotency_key is not None else None
        with self._runtime.lease_lock:
            if lease.identity.internal_restricted:
                raise PermissionError("Cannot enqueue background actions from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            existing = next(
                (c.continuation_id for c in lease.staging.continuations if c.idempotency_key == key and key is not None),
                None,
            )
            if existing is None and key is not None:
                existing = self._runtime.require_store().background_action_id_for_idempotency(lease.identity.session_id, lease.identity.owner, key)
            if existing is not None:
                return existing
            if not isinstance(lease.identity.root_action_id, str) or not lease.identity.root_action_id:
                raise RuntimeError("background action requires a captured root action identity")
            # Same deterministic identity rule as continuations: two concurrent leases that
            # both stage before either commits return the SAME id, and the store keeps one row.
            cid = uuid4().hex if key is None else hashlib.sha256("\0".join((identity.session_id, identity.owner, key)).encode("utf-8")).hexdigest()
            lease.staging.op_counter += 1
            authority = None
            if lease.identity.origin_visible_tools is not None and lease.identity.origin_readonly is not None:
                authority = {
                    "visible_tools": list(lease.identity.origin_visible_tools),
                    "readonly": lease.identity.origin_readonly,
                    "revision_id": lease.identity.revision_id,
                }
            staged_payload = {
                "action": owned,
                "root_action_budget": lease.identity.initial_root_budget,
                "tool_wait_authority": authority,
            }
            # The row stores the wrapped payload, so the bound measures the wrapper: an
            # oversized action fails here, before staging, instead of at commit time.
            continuation_size = len(canonical_json(staged_payload).encode("utf-8"))
            if continuation_size > MAX_CONTINUATION_PAYLOAD_BYTES:
                raise ValueError(
                    f"continuation payload is {continuation_size} bytes, "
                    f"exceeding the {MAX_CONTINUATION_PAYLOAD_BYTES}-byte continuation-payload bound"
                )
            lease.staging.continuations.append(
                StagedContinuationDraft(
                    continuation_id=cid,
                    owner=lease.identity.owner,
                    source=BACKGROUND_ACTION_SOURCE,
                    payload=staged_payload,
                    idempotency_key=key,
                    ordinal=lease.staging.op_counter,
                    root_id=lease.identity.root_action_id,
                    role="system",
                )
            )
        self._runtime.notify_background_action()
        return cid

    def cancel_background_action(self, action_id: str, reason: str = "owner cancellation") -> bool:
        lease = self._active_lease()
        self._check_hook("cancel_background_action")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("action_id must be a non-empty string")
        callback = self._runtime.cancel_background_action_callback
        if callback is None:
            raise RuntimeError("background action cancellation is unavailable")
        return callback(lease.identity.session_id, lease.identity.owner, action_id, str(reason))

    def background_action_status(self, action_id: str) -> BackgroundActionStatus | None:
        lease = self._active_lease()
        self._check_hook("background_action_status")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("action_id must be a non-empty string")
        record = self._runtime.require_store().background_action(action_id)
        if record is None or record.owner != lease.identity.owner or record.session_id != lease.identity.session_id:
            return None
        return self._runtime.background_action_status(record)
