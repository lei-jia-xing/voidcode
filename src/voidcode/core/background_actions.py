"""Background-action claim lifecycle and live-driver registry.

The registry owns the in-process state for claimed background actions: which action ids
this process is driving, each driver's abort event and stop cause, the condition callers
wait on, and the claim loop. It does not execute tools: the host core implements
`drive_background_action`, which runs the action's owner callback and commits through
the ordinary governed path.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from ..sdk.hooks import StopCause
from .plugins import PluginRuntime
from .storage.store import ContinuationRecord, SessionRecord, SqliteStore, StoreConflict


class BackgroundSnapshot(Protocol):
    @property
    def plugin_runtime(self) -> PluginRuntime: ...


class BackgroundActionHost(Protocol):
    """The core capabilities the registry may use, stated explicitly."""

    @property
    def workspace(self) -> Path: ...

    @property
    def store(self) -> SqliteStore: ...

    @property
    def closed(self) -> bool: ...

    @property
    def snapshot(self) -> BackgroundSnapshot | None: ...

    def ensure_open(self) -> None: ...

    def session(self, session_id: str) -> SessionRecord: ...

    def drive_background_action(self, claimed: ContinuationRecord, abort: threading.Event) -> None: ...

    def sweep_expired_asks(self) -> None: ...

    def cancel_background_tool_requests(
        self,
        session_id: str,
        action_id: str,
        *,
        status: Literal["cancelled", "interrupted", "core_lost", "budget_exhausted"],
        stop_cause: StopCause | None,
        reason: str,
    ) -> None: ...


class BackgroundActionRegistry:
    """Own the claim loop and every live-driver handle for one core process."""

    def __init__(self, host: BackgroundActionHost) -> None:
        self._host = host
        self._live: set[str] = set()
        self._claiming = 0
        self._aborts: dict[str, threading.Event] = {}
        self._abort_causes: dict[str, StopCause | None] = {}
        self._cond = threading.Condition()
        self._tool_wait_cond = threading.Condition()
        self._identity = {"pid": os.getpid(), "core": uuid4().hex}

    def wire(self, runtime: PluginRuntime) -> None:
        """Bind one runtime's background projections to this process's live driver registry."""
        runtime.background_action_notify_callback = self.notify
        runtime.background_claim_live_callback = self.claim_live
        with self._cond:
            live = set(self._live)
        runtime.background_live_actions = live

    def notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def notify_tool_waiters(self) -> None:
        with self._tool_wait_cond:
            self._tool_wait_cond.notify_all()

    def claim_live(self, record: ContinuationRecord) -> bool:
        """Whether one claimed background row is driven by this process right now."""
        with self._cond:
            return record.continuation_id in self._live

    def live_actions(self) -> set[str]:
        with self._cond:
            return set(self._live)

    def is_live(self, action_id: str) -> bool:
        with self._cond:
            return action_id in self._live

    def abort_event(self, action_id: str) -> threading.Event | None:
        with self._cond:
            return self._aborts.get(action_id)

    def abort_cause(self, action_id: str) -> StopCause | None:
        with self._cond:
            return self._abort_causes.get(action_id)

    def stop_cause(self, action_id: str) -> StopCause:
        with self._cond:
            return self._abort_causes.get(action_id) or "shutdown"

    def park(self, reason: str) -> None:
        """Signal every live driver to stop at its next invoke boundary, then wait for them.

        Drivers settle their own rows (cancelled); rows whose driver never reaches a
        boundary stay claimed-but-unattended and report `interrupted`. Nothing is
        auto-replayed: resume is always an explicit new action.
        """
        _ = reason
        self._signal_stop("shutdown")
        deadline = time.monotonic() + 30.0
        with self._cond:
            while self._live and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self._cond.wait(timeout=min(remaining, 0.2))

    def quiesce(self, reason: str) -> None:
        """Signal and wait until no driver or in-flight claim remains (core shutdown)."""
        _ = reason
        self._signal_stop("shutdown")
        with self._cond:
            while self._live or self._claiming:
                self._cond.wait()

    def signal_stop(self, reason: str) -> None:
        """Set every live driver's abort cause without waiting for them to settle."""
        _ = reason
        self._signal_stop("shutdown")

    def _signal_stop(self, cause: StopCause) -> None:
        with self._cond:
            for action_id, abort in self._aborts.items():
                self._abort_causes[action_id] = cause
                abort.set()
            self._cond.notify_all()
        self.notify_tool_waiters()

    def settle_driver(self, action_id: str) -> None:
        """Forget one finished driver and wake every lifecycle/tool waiter."""
        with self._cond:
            self._live.discard(action_id)
            self._abort_causes.pop(action_id, None)
            self._cond.notify_all()
        self.notify_tool_waiters()

    def wait_for_tool_outcome[T](self, check: Callable[[], T | None]) -> T | None:
        """Atomically check for a tool-wait outcome and wait one notification for it."""
        with self._tool_wait_cond:
            outcome = check()
            if outcome is not None:
                return outcome
            self._tool_wait_cond.wait()
            return check()

    def start(self) -> threading.Thread:
        """Start the claim loop and return its worker thread."""
        worker = threading.Thread(target=self._loop, name="CoreBackgroundLoop", daemon=True)
        worker.start()
        return worker

    def _loop(self) -> None:
        """Claim pending background actions and drive each on its own thread."""
        host = self._host
        while True:
            with self._cond:
                if host.closed:
                    return
                self._claiming += 1
            try:
                claimed = host.store.claim_background_action(workspace=str(host.workspace), claim_identity=self._identity)
            except StoreConflict:
                # Lost a claim race; back off and retry. Anything else is a real
                # store/contract failure and stays visible instead of idling as "no work".
                claimed = None
            except BaseException:
                with self._cond:
                    self._claiming -= 1
                    self._cond.notify_all()
                raise
            if claimed is None:
                with self._cond:
                    self._claiming -= 1
                    self._cond.notify_all()
                    if host.closed:
                        return
                    self._cond.wait(timeout=1.0)
                # Existing 1s idle wake (no new thread): settle caller-declared ask
                # expiries due now. Never kills the loop; per-session containment inside.
                if not host.closed:
                    host.sweep_expired_asks()
                continue
            abort = threading.Event()
            with self._cond:
                self._live.add(claimed.continuation_id)
                self._aborts[claimed.continuation_id] = abort
                self._abort_causes[claimed.continuation_id] = "shutdown" if host.closed else None
                if host.closed:
                    abort.set()
                self._claiming -= 1
                self._cond.notify_all()
            snapshot = host.snapshot
            if snapshot is not None:
                snapshot.plugin_runtime.background_live_actions.add(claimed.continuation_id)
            worker = threading.Thread(
                target=host.drive_background_action,
                args=(claimed, abort),
                name=f"CoreBackground-{claimed.continuation_id[:8]}",
                daemon=True,
            )
            worker.start()

    def cancel_action(self, session_id: str, owner: str, action_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel one owner action and only its own queued or in-flight tool requests."""
        host = self._host
        host.ensure_open()
        record = host.store.background_action(action_id)
        if record is None or record.owner != owner or record.session_id != session_id:
            return False
        if record.status in ("completed", "failed", "cancelled"):
            return False
        with self._cond:
            abort = self._aborts.get(action_id)
            live = action_id in self._live
            if live and abort is not None:
                self._abort_causes[action_id] = "owner_cancel"
                abort.set()
                self._cond.notify_all()
        if live and abort is not None:
            self.notify_tool_waiters()
            host.cancel_background_tool_requests(
                session_id,
                action_id,
                status="cancelled",
                stop_cause="owner_cancel",
                reason=reason,
            )
            return True
        host.cancel_background_tool_requests(
            session_id,
            action_id,
            status="cancelled",
            stop_cause="owner_cancel",
            reason=reason,
        )
        try:
            host.store.complete_background_action(action_id, status="cancelled")
        except StoreConflict:
            return False
        with self._cond:
            self._cond.notify_all()
        self.notify_tool_waiters()
        return True

    def wait_actions(
        self,
        session_id: str,
        *,
        action_id: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> tuple[dict[str, object], ...]:
        """Wait until this session's background actions settle; raise when one failed."""
        host = self._host
        host.ensure_open()
        host.session(session_id)
        if action_id is not None:
            return (self._wait_one(session_id, action_id, timeout_seconds=timeout_seconds),)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            pending = host.store.pending_background_actions(session_id)
            with self._cond:
                live = set(self._live)
            waited = [record for record in pending if record.status == "pending" or record.continuation_id in live]
            if not waited:
                # A claimed row outside the live set is stale — unless a claim is
                # in flight, in which case its completion notifies this cond.
                with self._cond:
                    if self._claiming > 0:
                        self._cond.wait(timeout=max(0.0, deadline - time.monotonic()))
                pending = host.store.pending_background_actions(session_id)
                with self._cond:
                    live = set(self._live)
                waited = [record for record in pending if record.status == "pending" or record.continuation_id in live]
                if waited:
                    continue
                return tuple(_stale_action_entry(record) for record in pending)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return tuple({"action_id": record.continuation_id, "status": record.status} for record in waited)
            with self._cond:
                self._cond.wait(timeout=min(remaining, 0.2))
            # One round settled (or was notified): surface failures before waiting further,
            # so a failed action never hides behind a longer-running sibling.
            for record in waited:
                current = host.store.background_action(record.continuation_id)
                if current is None or current.status != "failed":
                    continue
                raise RuntimeError(f"background action {current.continuation_id!r} failed" + _action_error_suffix(current))

    def _wait_one(self, session_id: str, action_id: str, *, timeout_seconds: float) -> dict[str, object]:
        """Wait for one exact action receipt, however it settled — even before this call."""
        store = self._host.store
        record = store.background_action(action_id)
        if record is None or record.session_id != session_id:
            raise ValueError(f"background action {action_id!r} does not belong to session {session_id!r}")
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            current = store.background_action(action_id)
            assert current is not None  # background rows are never deleted
            if current.status in ("completed", "failed", "cancelled"):
                if current.status == "failed":
                    raise RuntimeError(f"background action {action_id!r} failed" + _action_error_suffix(current))
                return {"action_id": action_id, "status": current.status}
            with self._cond:
                live = action_id in self._live
            if current.status == "claimed" and not live:
                with self._cond:
                    if self._claiming > 0:
                        self._cond.wait(timeout=max(0.0, deadline - time.monotonic()))
                reconfirmed = store.background_action(action_id)
                assert reconfirmed is not None
                with self._cond:
                    live = action_id in self._live
                if reconfirmed.status == "claimed" and not live:
                    return _stale_action_entry(reconfirmed)
                continue
            if time.monotonic() >= deadline:
                return {"action_id": action_id, "status": current.status}
            with self._cond:
                self._cond.wait(timeout=0.2)


def _stale_action_entry(record: ContinuationRecord) -> dict[str, object]:
    """The honest report for a claimed-but-unattended row: interrupted, never success."""
    error = record.payload.get("error")
    return {"action_id": record.continuation_id, "status": "interrupted", "error": error if isinstance(error, str) else None}


def _action_error_suffix(record: ContinuationRecord) -> str:
    """The recorded driver error of one failed row, as a raise suffix (empty when absent)."""
    error = record.payload.get("error")
    if isinstance(error, str) and error:
        return f": {error}"
    return ""
