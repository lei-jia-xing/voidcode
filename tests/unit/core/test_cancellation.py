"""Engine Cancellation subscription: one-shot, immediate-after-cancel, thread-safe."""

from __future__ import annotations

import threading
from pathlib import Path

from voidcode.bootstrap import open_core
from voidcode.core.cancellation import CURRENT, RUN_BOUND, Cancellation, CancellationCoordinator
from voidcode.core.engine import Core
from voidcode.core.storage.store import EventDraft, SessionStatus


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


def _open_core(tmp_path: Path) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return open_core(workspace, state_dir=tmp_path / "state")


def _commit_live_run(core: Core, session_id: str, run_id: str) -> None:
    """Harness commit: bind the session's durable run identity without driving it."""
    session = core.session(session_id)
    core.store.commit_staged(
        session_id,
        expected_sequence=session.last_event_sequence,
        expected_run_id=session.run_id,
        run_id=run_id,
        revision_id=core.revision_id,
        events=(EventDraft("core.observer_error", "core", "system", {"error": "cancellation harness"}),),
        status=SessionStatus.RUNNING,
    )


def test_current_target_signals_the_run_and_every_live_command() -> None:
    coordinator = CancellationCoordinator()
    run = Cancellation()
    first = Cancellation()
    second = Cancellation()
    coordinator.register_run("s", "run-1", run)
    coordinator.register_command("s", "first", first)
    coordinator.register_command("s", "second", second)

    selected = coordinator.signal("s", CURRENT, reason="stop", cause="user_stop")

    assert selected.abort is run
    assert selected.signalled == 3
    assert not selected.blocked
    assert (run.cancelled, first.cancelled, second.cancelled) == (True, True, True)
    assert {run.reason, first.reason, second.reason} == {"stop"}


def test_run_bound_none_is_a_reserved_binding_not_the_current_slot() -> None:
    coordinator = CancellationCoordinator()
    reserved = Cancellation()
    coordinator.register_run("s", None, reserved)

    stale = coordinator.signal("s", RUN_BOUND("run-1"), reason="stop", cause="owner_cancel")
    assert stale.blocked is True
    assert stale.signalled == 0
    assert reserved.cancelled is False

    selected = coordinator.signal("s", RUN_BOUND(None), reason="stop", cause="owner_cancel")
    assert selected.abort is reserved
    assert selected.signalled == 1
    assert reserved.cancelled is True
    assert reserved.stop_cause == "owner_cancel"


def test_bound_target_refuses_another_live_run_and_its_commands() -> None:
    coordinator = CancellationCoordinator()
    live = Cancellation()
    command = Cancellation()
    coordinator.register_run("s", "run-1", live)
    coordinator.register_command("s", "call", command)

    wrong = coordinator.signal("s", RUN_BOUND(None), reason="stop", cause="user_stop")
    assert wrong.blocked is True
    assert wrong.signalled == 0
    assert live.cancelled is False
    assert command.cancelled is False

    selected = coordinator.signal("s", RUN_BOUND("run-1"), reason="stop", cause="user_stop")
    assert selected.abort is live
    assert selected.signalled == 2
    assert live.cancelled is True
    assert command.cancelled is True


def test_bound_target_without_a_live_run_leaves_commands_alone() -> None:
    coordinator = CancellationCoordinator()
    command = Cancellation()
    coordinator.register_command("s", "call", command)

    selected = coordinator.signal("s", RUN_BOUND("run-1"), reason="stop", cause="user_stop")
    assert selected.abort is None
    assert selected.signalled == 0
    assert selected.blocked is False
    assert command.cancelled is False

    current = coordinator.signal("s", CURRENT, reason="stop", cause="user_stop")
    assert current.signalled == 1
    assert command.cancelled is True


def test_concurrent_command_aborts_register_and_signal_once() -> None:
    coordinator = CancellationCoordinator()
    run = Cancellation()
    coordinator.register_run("s", "run-1", run)
    signals = [Cancellation() for _ in range(32)]
    barrier = threading.Barrier(len(signals))

    def _register(index: int) -> None:
        barrier.wait(timeout=10)
        coordinator.register_command("s", f"invocation-{index}", signals[index])

    threads = [threading.Thread(target=_register, args=(index,)) for index in range(len(signals))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    selected = coordinator.signal("s", CURRENT, reason="stop", cause="user_stop")
    assert selected.abort is run
    assert selected.signalled == len(signals) + 1
    assert all(signal.cancelled for signal in signals)

    def _unregister(index: int) -> None:
        coordinator.unregister_command("s", f"invocation-{index}")

    threads = [threading.Thread(target=_unregister, args=(index,)) for index in range(len(signals))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    coordinator.unregister_run("s")
    assert coordinator.signal("s", CURRENT, reason="stop", cause="user_stop").signalled == 0


def test_cancel_session_current_and_bound_run_identities(tmp_path: Path) -> None:
    with _open_core(tmp_path) as core:
        core.create_session("s")
        reserved = Cancellation()
        core._cancellations.register_run("s", None, reserved)
        # A fresh session is durably bound to the reserved, never-started slot.
        assert core.cancel_session("s", target=RUN_BOUND("run-1")) is False
        assert reserved.cancelled is False
        assert core.cancel_session("s", target=RUN_BOUND(None)) is True
        assert reserved.cancelled is True

    with _open_core(tmp_path / "second") as core:
        core.create_session("s")
        _commit_live_run(core, "s", "run-1")
        live = Cancellation()
        core._cancellations.register_run("s", "run-1", live)
        # Each binding names its own slot, never whichever entry is live.
        assert core.cancel_session("s", target=RUN_BOUND(None)) is False
        assert live.cancelled is False
        assert core.cancel_session("s", target=RUN_BOUND("run-1")) is True
        assert live.cancelled is True


def test_stale_bound_cancel_touches_no_signal(tmp_path: Path) -> None:
    with _open_core(tmp_path) as core:
        core.create_session("s")
        stale = Cancellation()
        command = Cancellation()
        core._cancellations.register_run("s", "retired-run", stale)
        core._cancellations.register_command("s", "live-command", command)
        # The durable session never reached "retired-run": the stale request
        # refuses before touching the stale run entry or the live command.
        assert core.cancel_session("s", target=RUN_BOUND("retired-run")) is False
        assert stale.cancelled is False
        assert command.cancelled is False


def test_close_signals_every_live_run_and_command_abort(tmp_path: Path) -> None:
    core = _open_core(tmp_path)
    core.create_session("s")
    run = Cancellation()
    command = Cancellation()
    core._cancellations.register_run("s", "run-1", run)
    core._cancellations.register_command("s", "invocation", command)
    core.close()
    assert run.cancelled is True and run.stop_cause == "shutdown"
    assert command.cancelled is True and command.stop_cause == "shutdown"
