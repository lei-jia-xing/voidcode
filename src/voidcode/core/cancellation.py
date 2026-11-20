"""Cancellation primitives shared by runs, invocations, and background drivers.

`Cancellation` is the one-shot, callback-notifying signal the core owns; `_EventAbortSignal`
is the read-mostly provider-facing view over one background action's shared abort event.
Both were extracted from `engine.py` unchanged: the core orchestrates when to cancel,
these types own the transition and its observers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from threading import Event

from ..sdk.hooks import StopCause

logger = logging.getLogger(__name__)


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
