"""Idle TUI must not wake: renders happen on input/events/timers only."""

from __future__ import annotations

import threading
import time
from typing import Any

from tests.fixtures.polling import wait_for
from voidcode.sdk.client import ClientCapabilities
from voidcode.tui.events import WakeEvent
from voidcode.tui.terminal_app import CoreTuiApp


class _IdleClient:
    capabilities = ClientCapabilities()


class _IdleTerminal:
    """Non-blocking stub: never has input, never EOF; records painted frames."""

    width = 100
    content_width = 100
    height = 24
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.frames: list[list[str]] = []

    def read_bytes(self, timeout: float | None = 0.0) -> bytes:
        return b""

    def drain_kitty_replies(self) -> tuple[()]:
        return ()

    def resize_pending(self) -> bool:
        return False

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.rows))

    def close(self) -> None:
        pass


def test_idle_main_loop_renders_once_then_sleeps() -> None:
    """No input/events/timers: exactly the initial paint, then silence (no 30 Hz wakeups)."""
    terminal = _IdleTerminal()
    app = CoreTuiApp(_IdleClient(), session_id="", terminal=terminal)
    app.running = True
    worker = threading.Thread(target=app._main_loop, daemon=True)
    worker.start()
    try:
        wait_for(lambda: len(terminal.frames) >= 1, what="the initial idle paint")
        settled = len(terminal.frames)
        time.sleep(0.4)
        assert len(terminal.frames) == settled
    finally:
        app.running = False
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_queued_frame_renders_while_idle() -> None:
    """One queued frame wakes an idle loop into exactly one more render."""
    terminal = _IdleTerminal()
    app = CoreTuiApp(_IdleClient(), session_id="", terminal=terminal)
    app.running = True
    worker = threading.Thread(target=app._main_loop, daemon=True)
    worker.start()
    try:
        wait_for(lambda: len(terminal.frames) >= 1, what="the initial idle paint")
        settled = len(terminal.frames)
        app.event_queue.put(WakeEvent())
        wait_for(lambda: len(terminal.frames) >= settled + 1, what="one frame after WakeEvent")
        assert len(terminal.frames) == settled + 1
    finally:
        app.running = False
        worker.join(timeout=5)
        assert not worker.is_alive()
