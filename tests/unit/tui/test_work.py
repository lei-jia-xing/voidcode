"""The app-owned work group is bounded, named, logged, and shut down as one unit."""

from __future__ import annotations

import logging
import threading

from voidcode.tui.work import WorkGroup


def test_submit_runs_named_operations_and_can_wait_idle() -> None:
    work = WorkGroup(max_workers=2)
    seen: list[str] = []
    work.submit("One", lambda: seen.append("one"))
    work.submit("Two", lambda: seen.append("two"))
    assert work.wait_idle(5.0) is True
    assert sorted(seen) == ["one", "two"]
    work.shutdown()


def test_task_failure_is_logged_with_its_name(caplog) -> None:
    work = WorkGroup(max_workers=1)
    with caplog.at_level(logging.ERROR, logger="voidcode.tui.work"):
        work.submit("Explodes", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert work.wait_idle(5.0) is True
    assert "TUI background task Explodes failed" in caplog.text
    assert "boom" in caplog.text
    work.shutdown()


def test_shutdown_rejects_new_work_and_joins_lifecycle_threads() -> None:
    work = WorkGroup(max_workers=1)
    stopped = threading.Event()

    def loop() -> None:
        stopped.wait(5)

    thread = work.start("Lifecycle", loop)
    assert thread is not None
    stopped.set()
    work.shutdown()
    assert not thread.is_alive()
    assert work.submit("Late", lambda: None) is None


def test_start_returns_none_after_shutdown() -> None:
    work = WorkGroup(max_workers=1)
    work.shutdown()
    assert work.start("Late", lambda: None) is None
    assert work.wait_idle(0.1) is True


def test_wait_idle_timeout_does_not_lie_about_pending_work() -> None:
    work = WorkGroup(max_workers=1)
    release = threading.Event()
    work.submit("Blocked", lambda: release.wait(5))
    assert work.wait_idle(0.05) is False
    release.set()
    assert work.wait_idle(5.0) is True
    work.shutdown()
