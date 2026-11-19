"""Engine Cancellation subscription: one-shot, immediate-after-cancel, thread-safe."""

from __future__ import annotations

import threading

from voidcode.core.engine import Cancellation


def test_cancel_callbacks_fire_once_per_registration() -> None:
    seen: list[str] = []
    signal = Cancellation()
    signal.on_cancel(lambda: seen.append("a"))
    signal.on_cancel(lambda: seen.append("b"))
    signal.set_cancelled(True, reason="test")
    signal.set_cancelled(True, reason="test")
    assert seen == ["a", "b"]
    assert signal.cancelled is True
    assert signal.reason == "test"


def test_register_after_cancel_fires_immediately() -> None:
    seen: list[str] = []
    signal = Cancellation()
    signal.set_cancelled(True, reason="test")
    unregister = signal.on_cancel(lambda: seen.append("late"))
    assert seen == ["late"]
    unregister()
    unregister()
    assert seen == ["late"]


def test_unregister_prevents_fire() -> None:
    seen: list[str] = []
    signal = Cancellation()
    unregister = signal.on_cancel(lambda: seen.append("dropped"))
    unregister()
    signal.set_cancelled(True, reason="test")
    assert seen == []


def test_callback_may_reenter_without_deadlock() -> None:
    """Collect-then-call proof: a firing callback can touch the signal without hanging."""
    events: list[str] = []
    signal = Cancellation()

    def _callback() -> None:
        events.append("fired")
        signal.set_cancelled(False)
        signal.on_cancel(lambda: events.append("nested"))

    signal.on_cancel(_callback)
    signal.set_cancelled(True)
    assert events == ["fired"]
    assert signal.cancelled is False
    signal.set_cancelled(True)
    assert events == ["fired", "nested"]


def test_concurrent_register_and_fire_delivers_exactly_once() -> None:
    signal = Cancellation()
    counts: dict[int, int] = {}
    guard = threading.Lock()

    def _register(index: int) -> None:
        def _callback() -> None:
            with guard:
                counts[index] = counts.get(index, 0) + 1

        signal.on_cancel(_callback)

    threads = [threading.Thread(target=_register, args=(index,)) for index in range(64)]
    for thread in threads:
        thread.start()
    signal.set_cancelled(True, reason="test")
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(counts) == list(range(64))
    assert set(counts.values()) == {1}
