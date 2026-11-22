"""Cancellation primitives shared by runs, invocations, and background drivers.

`Cancellation` is the one-shot, callback-notifying signal the core owns; `_EventAbortSignal`
is the read-mostly provider-facing view over one background action's shared abort event.
`CancelTarget` names which live signal a cancellation request may touch, and
`CancellationCoordinator` owns the in-process abort registry without reading durable
state. Both signals were extracted from `engine.py` unchanged: the core orchestrates
when to cancel, these types own the transition and its observers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock

from ..sdk.hooks import StopCause

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CurrentTarget:
    """Signal whichever run currently owns the session, plus its live commands."""


@dataclass(frozen=True, slots=True)
class RunBoundTarget:
    """Signal only the run registered under one exact run identity.

    ``run_id=None`` is a real binding to a reserved, never-started run and stays
    distinct from an unbound, session-named request.
    """

    run_id: str | None


type CancelTarget = CurrentTarget | RunBoundTarget

#: Unbound cancellation: the session names the target, not a run identity.
CURRENT = CurrentTarget()


def RUN_BOUND(run_id: str | None) -> RunBoundTarget:
    """Bind a cancellation to one exact live run (``None``: reserved, never started)."""
    return RunBoundTarget(run_id)


class Cancellation:
    def __init__(
        self,
        linked_event: threading.Event | None = None,
        *,
        linked_reason: str = "background action cancelled",
        linked_stop_cause: StopCause = "owner_cancel",
        linked_stop_cause_provider: Callable[[], StopCause] | None = None,
    ) -> None:
        self._event = Event()
        self._reason: str | None = None
        self._lock = threading.Lock()
        self._cancel_callbacks: list[Callable[[], None]] = []
        self._stop_cause: StopCause = "user_stop"
        self._linked_event = linked_event
        self._linked_reason = linked_reason
        self._linked_stop_cause_provider = linked_stop_cause_provider
        self._linked_stop_cause = linked_stop_cause

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or (self._linked_event is not None and self._linked_event.is_set())

    @property
    def reason(self) -> str | None:
        with self._lock:
            if self._event.is_set():
                return self._reason
        return self._linked_reason if self._linked_event is not None and self._linked_event.is_set() else None

    @property
    def stop_cause(self) -> StopCause:
        with self._lock:
            if self._event.is_set():
                return self._stop_cause
        if self._linked_event is None or not self._linked_event.is_set():
            return "user_stop"
        if self._linked_stop_cause_provider is not None:
            return self._linked_stop_cause_provider()
        return self._linked_stop_cause

    def set_cancelled(self, value: bool, *, reason: str | None = None, stop_cause: StopCause = "user_stop") -> None:
        with self._lock:
            self._reason = reason if value else None
            self._stop_cause = stop_cause if value else "user_stop"
            firing: tuple[Callable[[], None], ...] = ()
            if value:
                self._event.set()
                firing = tuple(self._cancel_callbacks)
                self._cancel_callbacks.clear()
            else:
                self._event.clear()
        for callback in firing:
            self._fire_cancel_callback(callback)

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a one-shot callback fired when this signal is set-cancelled.

        Callbacks run outside the internal lock, each at most once; registering on
        an already-cancelled signal fires immediately. Returns an idempotent
        unregister handle.
        """
        with self._lock:
            already = self._event.is_set() or (self._linked_event is not None and self._linked_event.is_set())
            if not already:
                self._cancel_callbacks.append(callback)

        def _unregister() -> None:
            with self._lock:
                try:
                    self._cancel_callbacks.remove(callback)
                except ValueError:
                    pass

        if already:
            self._fire_cancel_callback(callback)
        return _unregister

    @staticmethod
    def _fire_cancel_callback(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            logger.exception("cancellation callback failed")


@dataclass(frozen=True, slots=True)
class CancelSelection:
    """The live aborts one cancellation request selected and signalled.

    ``abort`` is the selected session-run signal, if any; ``signalled`` counts
    every signal this request set. ``blocked`` means a bound target found a
    live entry for another run and touched nothing, commands included.
    """

    abort: Cancellation | None
    signalled: int
    blocked: bool = False


class CancellationCoordinator:
    """The in-process abort registry for one core.

    It owns one live run abort per session, keyed by the run identity it cancels,
    plus one abort per concurrently running command invocation. Durable ownership,
    run identity, and pending-continuation settlement stay with the core: this
    registry never reads the store and never calls back into it.

    Matching and signalling happen under one lock, so a bound request either
    signals the registered entry it names or touches no signal at all.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._session_aborts: dict[str, tuple[str | None, Cancellation]] = {}
        self._command_aborts: dict[str, dict[str, Cancellation]] = {}

    def register_run(self, session_id: str, run_id: str | None, abort: Cancellation) -> None:
        """Install the live signal for one run; a newer run replaces the prior entry."""
        with self._lock:
            self._session_aborts[session_id] = (run_id, abort)

    def unregister_run(self, session_id: str) -> None:
        """Drop the session's live run signal after its driver leaves."""
        with self._lock:
            self._session_aborts.pop(session_id, None)

    def active_abort(self, session_id: str) -> Cancellation | None:
        """The session's live run signal, if a driver currently holds one."""
        with self._lock:
            entry = self._session_aborts.get(session_id)
            return None if entry is None else entry[1]

    def has_abort(self, session_id: str) -> bool:
        """Whether a live run signal currently owns this session."""
        with self._lock:
            return session_id in self._session_aborts

    def register_command(self, session_id: str, invocation_id: str, abort: Cancellation) -> None:
        """Install one command invocation's signal beside any live run signal."""
        with self._lock:
            self._command_aborts.setdefault(session_id, {})[invocation_id] = abort

    def unregister_command(self, session_id: str, invocation_id: str) -> None:
        """Drop one command invocation's signal, keeping its siblings live."""
        with self._lock:
            commands = self._command_aborts.get(session_id)
            if commands is None:
                return
            commands.pop(invocation_id, None)
            if not commands:
                self._command_aborts.pop(session_id, None)

    def signal(self, session_id: str, target: CancelTarget, *, reason: str, cause: StopCause) -> CancelSelection:
        """Select and signal exactly the abort entries ``target`` names.

        A bound target refuses when a live entry belongs to another run and
        touches nothing; with no live entry it selects nothing and leaves
        beside-running commands alone. An unbound target signals the live run
        (when present) and every live command of the session.
        """
        with self._lock:
            entry = self._session_aborts.get(session_id)
            abort: Cancellation | None = None
            if entry is not None and (isinstance(target, CurrentTarget) or entry[0] == target.run_id):
                abort = entry[1]
            if isinstance(target, RunBoundTarget) and entry is not None and abort is None:
                return CancelSelection(None, 0, blocked=True)
            signals: list[Cancellation] = []
            if not (isinstance(target, RunBoundTarget) and abort is None):
                signals.extend(self._command_aborts.get(session_id, {}).values())
            if abort is not None:
                signals.append(abort)
        for signal in signals:
            signal.set_cancelled(True, reason=reason, stop_cause=cause)
        return CancelSelection(abort, len(signals))

    def session_ids(self) -> tuple[str, ...]:
        """Sessions holding a live run signal, for durable settlement by the caller."""
        with self._lock:
            return tuple(self._session_aborts)

    def signal_all(self, *, reason: str, cause: StopCause) -> None:
        """Signal every live run and command abort (owner shutdown)."""
        with self._lock:
            signals = [abort for _, abort in self._session_aborts.values()]
            for commands in self._command_aborts.values():
                signals.extend(commands.values())
        for signal in signals:
            signal.set_cancelled(True, reason=reason, stop_cause=cause)


class _EventAbortSignal:
    """A `ProviderAbortSignal` view over one background action's abort event.

    The driver owns the event; callbacks only observe it. `set_cancelled` mirrors into
    the shared event so cooperative tools can signal, but clearing is refused: an abort
    once observed must stay observed for every later invoke of the same action.
    """

    def __init__(self, event: threading.Event) -> None:
        self._event = event
        self._reason: str | None = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        with self._lock:
            if value:
                self._reason = reason
                self._event.set()
            elif not self._event.is_set():
                self._reason = None
