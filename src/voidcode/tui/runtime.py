"""Extracted CoreTuiApp behavior: runtime.

Methods are applied to the app through RuntimeActions mixins; the facade stays in
terminal_app.py and owns composition, not these responsibilities.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from . import catalog, events, operations
from .event_pump import drain_events
from .rendering import QUIT_ARM_SECONDS
from .rendering import banner_rows as _banner_rows
from .views import (
    InteractiveChoices,
    InteractiveMarkdown,
    render_assistant_markdown,
)

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from .terminal_app import CoreTuiApp


#: Fallback live-view cadence when a reply carries no explicit refresh interval.
_LIVE_REFRESH_FALLBACK_SECONDS = 60.0
#: Spinner/live cadence while animations run; matches the pre-cutover 30 fps loop.
_SPINNER_TICK_SECONDS = 1 / 30
#: Retry nap for instant-returning read doubles.
_INPUT_RETRY_SECONDS = 0.01
#: Input-fd read slice: bounds each locked terminal read.
_INPUT_READ_SLICE_SECONDS = 0.05
#: Idle queue-block ceiling.
_MAX_IDLE_BLOCK_SECONDS = 1.0


class RuntimeActions:
    def run(self: CoreTuiApp) -> int:
        self.running = True
        try:
            self._init_session()
            catalog.load(self)
            self._maybe_welcome()
            self._start_event_stream()
            return self._main_loop()
        finally:
            self.running = False
            self._stop_event.set()
            self._input_stop.set()
            # Cancel the stream before shutdown joins its worker: an idle follow
            # stream is waiting on async network events, and cancellation wakes it.
            self._completion.cancel()
            try:
                if self._stream_cancel is not None:
                    self._stream_cancel.close()
            finally:
                self._work.shutdown()
                self._artifact_exports.cleanup()
                if self.owns_terminal:
                    self.terminal.close()

    def _init_session(self: CoreTuiApp) -> None:
        if self.session_id:
            # Explicit requested session: must exist on the backend; never silently create new
            record = self.client.get_session(self.session_id)
            actual_id = record.session_id
            if not actual_id:
                raise RuntimeError(f"Server returned invalid session record for {self.session_id!r}")
            self.session_id = actual_id
        else:
            # No session requested: list sessions; if none exist, create one
            sessions = self.client.list_sessions(limit=1)
            if sessions:
                actual_id = sessions[0].session_id
                if not actual_id:
                    raise RuntimeError("Server returned session without session_id")
                self.session_id = actual_id
            else:
                new_session = self.client.create_session()
                actual_id = new_session.session_id
                if not actual_id:
                    raise RuntimeError("Server created session without session_id")
                self.session_id = actual_id
        # Routine attachment is quiet: ids are diagnostics, not idle chrome.
        self.status_line = ""

    def _maybe_welcome(self: CoreTuiApp) -> None:
        """Commit the welcome mark once when the attached session is durably empty."""
        if self.session_id in self._welcomed_sessions or self._session_cursors.get(self.session_id, 0):
            return
        try:
            if self.client.get_session(self.session_id).last_event_sequence:
                self._welcomed_sessions.add(self.session_id)
                return
        except Exception:
            return
        rows = _banner_rows(self.banner, self.styles, width=self.terminal.width)
        self._welcomed_sessions.add(self.session_id)
        if rows:
            self.terminal.commit_rows(rows)

    def _start_event_stream(self: CoreTuiApp) -> None:
        """Begin following the current session; the follower owns thread + cancel lifecycle."""
        self._follower.start_stream(self.session_id)

    def _note_stream_reconnect(self: CoreTuiApp) -> None:
        """Clear stream readiness; the follower calls this on every reconnect."""
        self._stream_ready = False

    def _main_loop(self: CoreTuiApp) -> int:
        self._start_input_thread()
        try:
            self._render_frame()
            while self.running:
                drained = self._drain_events(timeout=self._idle_timeout())
                fired = self._fire_due_timers()
                if drained or fired or self._animations_live():
                    self._render_frame()
        finally:
            self._input_stop.set()
            thread, self._input_thread = self._input_thread, None
            if thread is not None:
                thread.join(timeout=1.0)
        return 0

    def _start_input_thread(self: CoreTuiApp) -> None:
        """Read terminal bytes on a daemon thread; the main loop stays in queue.get."""
        self._input_stop.clear()

        def worker() -> None:
            while not self._input_stop.is_set():
                try:
                    with self._terminal_read_lock:
                        chunk = self.terminal.read_bytes(timeout=_INPUT_READ_SLICE_SECONDS)
                        replies = list(self.terminal.drain_kitty_replies())
                except Exception:
                    if self._input_stop.is_set():
                        return
                    time.sleep(_INPUT_RETRY_SECONDS)
                    continue
                if not chunk and not replies:
                    if self._input_stop.is_set():
                        return
                    if self.terminal.eof:
                        self.event_queue.put(events.InputEofEvent())
                        return
                    time.sleep(_INPUT_RETRY_SECONDS)
                    continue
                if chunk:
                    self.event_queue.put(events.InputEvent(chunk=chunk, arrived_at=time.monotonic()))
                for reply in replies:
                    self.event_queue.put(events.KittyReplyEvent(reply=reply))

        self._input_thread = self._work.start("Input", worker)
        if self._input_thread is None:
            raise RuntimeError("TUI work group is already shut down")

    def _animations_live(self: CoreTuiApp) -> bool:
        return bool(self._run_active or self._stream_dispatch is not None or self._active_tools)

    def _live_refresh_armed(self: CoreTuiApp) -> bool:
        """Whether a live refresh would fire now apart from its deadline (mirror of _poll_live_input's gates)."""
        live = self._live_input
        if live is None or self._live_refresh_at <= 0.0:
            return False
        if self.pending_approval is not None or not isinstance(self.active_overlay, (InteractiveChoices, InteractiveMarkdown)):
            return False
        return getattr(self.active_overlay, "revision_id", "") == live.revision

    def _idle_timeout(self: CoreTuiApp) -> float:
        """Seconds until the next timer fire; fully idle parks on the liveness snooze."""
        now = time.monotonic()
        best: float | None = None

        def consider(at: float) -> None:
            nonlocal best
            wait = max(0.0, at - now)
            best = wait if best is None else min(best, wait)

        if self._escape_flush_at:
            consider(self._escape_flush_at)
        if self._live_refresh_armed():
            consider(self._live_refresh_at)
        if self._live_views_armed():
            consider(self._live_views_refresh_at)
        if self._artifact_ack_deadline is not None:
            consider(self._artifact_ack_deadline[2])
        if self._quit_armed_at != float("-inf"):
            consider(self._quit_armed_at + QUIT_ARM_SECONDS)
        if self._animations_live():
            consider(now + _SPINNER_TICK_SECONDS)
        # Fully idle: a long liveness snooze, not a spin. Real events, input,
        # and the shutdown wake return from queue.get immediately; renders
        # still happen only on drained frames, fired timers, or animations.
        return min(best, _MAX_IDLE_BLOCK_SECONDS) if best is not None else _MAX_IDLE_BLOCK_SECONDS

    def _fire_due_timers(self: CoreTuiApp) -> bool:
        """Run due timer actions; returns True when visible state may have changed."""
        now = time.monotonic()
        fired = False
        self._poll_live_input()
        self._poll_live_views()
        if self._handle_input_bytes(b""):
            fired = True
        if self.terminal.resize_pending():
            # A native resize arrived mid-park: force the render that consumes it.
            fired = True
        if self._quit_armed_at != float("-inf") and now >= self._quit_armed_at + QUIT_ARM_SECONDS:
            self._quit_armed_at = float("-inf")
            fired = True
        deadline = self._artifact_ack_deadline
        if deadline is not None and now >= deadline[2]:
            fired = True
        return fired

    def _poll_live_input(self: CoreTuiApp) -> None:
        """Fire one live refresh when its deadline passes and the view still owns focus."""
        live = self._live_input
        if live is None or self._live_refresh_at <= 0.0:
            return
        if time.monotonic() < self._live_refresh_at:
            return
        if self.pending_approval is not None or not isinstance(self.active_overlay, (InteractiveChoices, InteractiveMarkdown)):
            return  # gate/editor owns focus: pause, keep the deadline so resume fires at once
        if getattr(self.active_overlay, "revision_id", "") != live.revision:
            self._live_input = None
            self._live_refresh_at = 0.0
            return
        self._live_refresh_at = time.monotonic() + _LIVE_REFRESH_FALLBACK_SECONDS  # rearmed by the reply; never stacks workers
        self._refresh_live_input_async()

    def _live_views_armed(self: CoreTuiApp) -> bool:
        return self._live_views_refresh_at > 0.0 and (self._run_active or bool(self._active_tools) or bool(self._live_views))

    def _poll_live_views(self: CoreTuiApp) -> None:
        if not self._live_views_armed():
            return
        if time.monotonic() < self._live_views_refresh_at:
            return
        self._live_views_refresh_at = time.monotonic() + _LIVE_REFRESH_FALLBACK_SECONDS
        self._refresh_live_views()

    def _refresh_live_views(self: CoreTuiApp) -> None:
        if self._live_views_inflight:
            return
        self._live_views_inflight = True
        session_id, generation = self.session_id, self._generation
        self._work.submit(
            "CoreLiveViews",
            lambda: self.event_queue.put(operations.load_live_views(self.client, session_id=session_id, generation=generation)),
        )

    def _live_stream_rows(self: CoreTuiApp, width: int) -> list[str]:
        """Partial markdown and reasoning of the in-flight dispatch for the live region."""
        dispatch = self._stream_dispatch
        if dispatch is None:
            return []
        reasoning_chunks = self._stream_reasoning.get(dispatch, [])
        reasoning_text = "".join(reasoning_chunks)
        text = "".join(self._stream_buffers.get(dispatch, []))
        if not text and not reasoning_text:
            return []
        rows: list[str] = []
        if reasoning_text:
            rows.append(self.styles.fg("dim", f"Thinking… ({len(reasoning_text)} chars)"))
        if text:
            rows.extend(render_assistant_markdown(text, width, self.styles))
        return rows

    def _drain_events(self: CoreTuiApp, timeout: float | None = None) -> int:
        """Drain typed frames through the one pump/router in ``event_pump``."""
        return drain_events(self, timeout)
