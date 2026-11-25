"""The lease gate every service mixin shares: liveness and hook observation limits."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..plugins import LeaseContext, PluginRuntime

#: Methods a hook lease may call: hooks observe, they do not stage state or start work.
_HOOK_METHODS: frozenset[str] = frozenset(
    {
        "get_state",
        "read_events",
        "read_facts",
        "read_transcript",
        "read_context_history",
        "read_child_history",
        "read_session_history",
        "list_descendants",
        "session_info",
        "get_usage",
        "get_budget",
        "read_artifact",
        "tool_definition",
        "overlay_active",
        "session_read_only",
    }
)


class ServiceLease:
    """One `RuntimeServices` instance bound to one lease for a single plugin callback."""

    _HOOK_METHODS: frozenset[str] = _HOOK_METHODS

    def __init__(self, runtime: PluginRuntime, lease: LeaseContext) -> None:
        self._runtime = runtime
        self._lease = lease

    @property
    def owner(self) -> str:
        return self._active_lease().identity.owner

    def _active_lease(self) -> LeaseContext:
        if self._lease.revoked or self._runtime.closed:
            raise PermissionError("Service lease was revoked")
        return self._lease

    def _check_hook(self, method: str) -> None:
        if self._lease.identity.hook and method not in self._HOOK_METHODS:
            raise PermissionError(f"hook leases cannot call {method}: hooks observe, they do not stage state or start work")
