"""Invocation leases: opening, revoking, cancellation mirroring, and staged-change freezing."""

from __future__ import annotations

import threading
from collections.abc import Callable
from uuid import uuid4

from voidcode.sdk.provider import (
    ProviderAbortSignal,
)

from ..storage.store import (
    StagedStateUpdate,
)
from ._catalog import PluginCatalog
from ._contracts import LeaseContext, LeaseIdentity, _Staged


class PluginLeases(PluginCatalog):
    """Invocation leases: opening, revoking, cancellation mirroring, and staged-change freezing."""

    @staticmethod
    def current_thread_has_active_lease() -> bool:
        """Whether this thread is inside any plugin callback lease, across generations."""
        with _ACTIVE_LEASE_THREADS_LOCK:
            return _ACTIVE_LEASE_THREADS.get(threading.get_ident(), 0) > 0

    def _open_lease(
        self,
        *,
        owner: str,
        session_id: str,
        run_id: str,
        invocation_id: str,
        readonly: bool,
        actor: str,
        hook: bool = False,
        internal_work: bool = False,
        invocation_depth: int = 0,
        tool_callback: bool = False,
        root_action_id: str | None = None,
        initial_root_budget: int | None = None,
        origin_visible_tools: tuple[str, ...] | None = None,
        origin_readonly: bool | None = None,
        background_action_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> LeaseContext:
        lease = LeaseContext(
            identity=LeaseIdentity(
                token=uuid4().hex,
                owner=owner,
                session_id=session_id,
                workspace=self.workspace,
                run_id=run_id,
                revision_id=self.revision_id,
                invocation_id=invocation_id,
                readonly=readonly,
                root_action_id=root_action_id,
                initial_root_budget=initial_root_budget,
                origin_visible_tools=origin_visible_tools,
                origin_readonly=origin_readonly,
                background_action_id=background_action_id,
                actor=actor,
                hook=hook,
                internal_work=internal_work,
                invocation_depth=invocation_depth,
                tool_callback=tool_callback,
            ),
            abort_signal=abort_signal,
        )
        with self._lease_lock:
            self._active_leases[lease.identity.token] = lease
        with _ACTIVE_LEASE_THREADS_LOCK:
            _ACTIVE_LEASE_THREADS[lease.opened_thread_id] = _ACTIVE_LEASE_THREADS.get(lease.opened_thread_id, 0) + 1
        signal = abort_signal
        if signal is not None:
            on_cancel = getattr(signal, "on_cancel", None)
            if on_cancel is not None:
                cancel_event = self.cancel_event_for(lease)

                def _mirror() -> None:
                    self._mirror_lease_cancel(lease, cancel_event)

                lease.cancel_subscription = on_cancel(_mirror)
        return lease

    def _revoke_lease(self, lease: LeaseContext) -> None:
        with self._lease_lock:
            active = self._active_leases.pop(lease.identity.token, None) is not None
            lease.revoked = True
            self._cancel_events.pop(lease.identity.token, None)
            self._claimed_services.pop(lease.identity.token, None)
            unsubscribe = lease.cancel_subscription
            lease.cancel_subscription = None
        if unsubscribe is not None:
            unsubscribe()
        if active:
            with _ACTIVE_LEASE_THREADS_LOCK:
                count = _ACTIVE_LEASE_THREADS.get(lease.opened_thread_id, 0)
                if count <= 1:
                    _ACTIVE_LEASE_THREADS.pop(lease.opened_thread_id, None)
                else:
                    _ACTIVE_LEASE_THREADS[lease.opened_thread_id] = count - 1
            if self._artifacts is not None:
                self._artifacts.discard_owner(lease.identity.token)

    def _staged_changes(self, lease: LeaseContext) -> _Staged:
        owner_schema_ver = self.state_schema_version(lease.identity.owner)
        return (
            tuple(
                StagedStateUpdate(
                    owner=lease.identity.owner,
                    scope=sc,
                    scope_key=sk,
                    payload=st,
                    expected_version=baseline,
                    schema_version=owner_schema_ver,
                    revision_id=self.revision_id,
                )
                for (sc, sk), (st, _local, baseline) in lease.staging.state.items()
            ),
            tuple(lease.staging.events),
            tuple(lease.staging.continuations),
            tuple(lease.staging.cancellations),
            tuple(lease.staging.observer_receipts),
            lease.staging.budget,
            tuple(lease.staging.wakeups),
            tuple(lease.staging.wakeup_cancels),
        )

    def cancel_event_for(self, lease: LeaseContext) -> threading.Event:
        """The cancellation event bound to one lease's invocation."""
        with self._lease_lock:
            event = self._cancel_events.get(lease.identity.token)
            if event is None:
                event = threading.Event()
                self._cancel_events[lease.identity.token] = event
            return event

    def admission_guard(self, lease: LeaseContext) -> Callable[[], bool]:
        """A store-transaction-safe liveness check for the caller's own lease.

        Captures the lease's own abort signal, cancel event, and revoked flag
        reader — never a fresh lookup of the session's current registry entry,
        so a stale token cannot borrow a newer run's signal and a fresh call
        after cancellation passes normally. The guard touches only captured
        in-memory state, so it is safe under the store lock (no aborts/store
        lookup, no lock inversion).
        """
        signal = lease.abort_signal
        event = self.cancel_event_for(lease)

        def _admitted() -> bool:
            return not (self._closed or lease.revoked or event.is_set() or (signal is not None and signal.cancelled))

        return _admitted

    def claim_service(self, lease: LeaseContext, process_id: str) -> None:
        """Bind one live *service* id to this invocation so cancellation stops its tree."""
        with self._lease_lock:
            self._claimed_services.setdefault(lease.identity.token, set()).add((process_id, lease.identity.session_id))

    def _cancel_claimed_services(self, lease: LeaseContext) -> None:
        with self._lease_lock:
            claims = tuple(self._claimed_services.pop(lease.identity.token, ()))
        for process_id, session_id in claims:
            # A live invocation's active services die with the cancellation: this is the same
            # cancellation a prompt plugin performs itself, just owned by the runtime so a reused
            # service cannot outlive the invocation that was using it.
            try:
                self.processes.cancel(process_id, owner=lease.identity.owner, session_id=session_id)
            except ValueError:
                pass

    def _mirror_lease_cancel(self, lease: LeaseContext, cancel_event: threading.Event) -> None:
        """Mirror an abort-signal cancel into the lease event and stop its claimed trees."""
        cancel_event.set()
        self._cancel_claimed_services(lease)


_ACTIVE_LEASE_THREADS_LOCK = threading.Lock()
_ACTIVE_LEASE_THREADS: dict[int, int] = {}
