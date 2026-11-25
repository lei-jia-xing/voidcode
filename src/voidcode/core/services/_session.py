"""Session-scoped projection windows and read-only overlays."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Literal

from ..events import EventType
from ._lease import ServiceLease


class SessionServices(ServiceLease):
    """Window projections, overlays, and the caller's read-only stance."""

    def project_window(self, *, covered_from_sequence: int, covered_to_sequence: int, replacement: str) -> None:
        lease = self._active_lease()
        self._check_hook("project_window")
        if lease.identity.internal_restricted:
            raise PermissionError("Cannot project a session window from a read-only service lease")
        if type(covered_from_sequence) is not int or type(covered_to_sequence) is not int:
            raise ValueError("projection boundaries must be integers")
        self._runtime.store_window_projection(lease, covered_from_sequence, covered_to_sequence, str(replacement))

    def window_projection(self) -> Mapping[str, object] | None:
        lease = self._active_lease()
        for event in reversed(lease.staging.events):
            if event.event_type == EventType.WINDOW_PROJECTED:
                return dict(event.payload)
        record = self._runtime.require_store().window_projection(lease.identity.session_id)
        return None if record is None else asdict(record)

    def set_overlay(self, tier: Literal["read-only"] = "read-only") -> None:
        lease = self._active_lease()
        self._check_hook("set_overlay")
        self._runtime.require_store().set_session_overlay(lease.identity.session_id, lease.identity.owner, str(tier))

    def clear_overlay(self) -> bool:
        lease = self._active_lease()
        self._check_hook("clear_overlay")
        return self._runtime.require_store().clear_session_overlay(lease.identity.session_id, lease.identity.owner)

    def overlay_active(self) -> bool:
        lease = self._active_lease()
        self._check_hook("overlay_active")
        return self._runtime.require_store().session_overlay(lease.identity.session_id, lease.identity.owner) is not None

    def session_read_only(self) -> bool:
        lease = self._active_lease()
        self._check_hook("session_read_only")
        callback = self._runtime.session_read_only_callback
        if callback is None:
            return bool(self._runtime.require_store().session_overlays(lease.identity.session_id))
        return callback(lease.identity.session_id)
