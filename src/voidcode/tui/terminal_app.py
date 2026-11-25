"""Interactive inline TUI application for the VoidCode core transport."""

from __future__ import annotations

import logging
import queue
import tempfile
import threading
from pathlib import Path
from typing import Literal

from voidcode.config import BannerConfig
from voidcode.sdk.client import (
    HealthStatus,
    QueuedFollowup,
    SessionClient,
    StreamCancellation,
)
from voidcode.sdk.commands import CommandDescriptor
from voidcode.sdk.views import (
    PluginViewDescriptor,
)
from voidcode.tui.gates import (
    GateState,
)

from . import catalog, events
from .artifact_actions import ArtifactActions
from .artifact_viewer import (
    ArtifactSource,
)
from .attachments import (
    PendingAttachments,
)
from .command_actions import CommandActions
from .completion import CompletionCatalog, CompletionController
from .composer import Composer
from .formatting import StatusLineKind
from .input_actions import InputActions
from .keys import KeyDecoder
from .overlay_actions import OverlayActions
from .overlays import OverlayStack
from .rendering import ActiveToolProgress, render_frame
from .runtime import RuntimeActions
from .session_actions import SessionActions
from .session_follower import SessionFollower
from .stream_actions import StreamActions
from .styles import Styles
from .term import Terminal
from .views import (
    InteractiveApproval,
)
from .work import WorkGroup

logger = logging.getLogger(__name__)

#: Control bytes inside model/plugin text must never reach the terminal as
#: commands (they could clear scrollback or switch buffers), so they are made
#: visible before the row is styled or committed.

#: A lone ESC byte is ambiguous (it may start a sequence) until no further input
#: follows: the decoder buffers it, and the loop resolves it after this window.
#: Matches the 50 ms window the pre-cutover client used.
_ESCAPE_FLUSH_SECONDS = 0.05

#: Fallback live-view cadence when a reply carries no explicit refresh interval;
#: only a reply rearms, so this never stacks workers.
_LIVE_REFRESH_FALLBACK_SECONDS = 60.0

#: One typed image path is bounded like any other client-side text buffer.
_MAX_ATTACHMENT_PATH_CHARS = 4096

#: Spinner/live cadence while animations run; matches the pre-cutover 30 fps render loop.
_SPINNER_TICK_SECONDS = 1 / 30
#: Retry nap for instant-returning read doubles (tests); real terminals block in read_bytes.
_INPUT_RETRY_SECONDS = 0.01

#: Input-fd read slice: bounds each locked terminal read so kitty probe/upload
#: queries never wait long for the input thread (concurrent readers would split replies).
_INPUT_READ_SLICE_SECONDS = 0.05
#: Idle queue-block ceiling: bounds the snooze when no timer is armed. Real
#: events, input, and the shutdown wake return from queue.get immediately; the
#: one-second ceiling bounds flag-only wakeups (SIGWINCH sets a flag the next
#: render consumes) without rendering while idle.
_MAX_IDLE_BLOCK_SECONDS = 1.0

#: Immutable ctor/entry default: None at the boundary always means disabled.
_DEFAULT_BANNER = BannerConfig()


class CoreTuiApp(
    ArtifactActions,
    CommandActions,
    InputActions,
    OverlayActions,
    RuntimeActions,
    SessionActions,
    StreamActions,
):
    """RPC-backed inline TUI using Terminal and TuiFrame."""

    def __init__(
        self,
        client: SessionClient,
        *,
        session_id: str | None = None,
        terminal: Terminal | None = None,
        border: Literal["lines", "box", "borderless"] = "lines",
        footer: Literal["full", "compact", "off"] = "full",
        show_session: bool = False,
        workspace_dir: object = None,
        banner: BannerConfig | None = _DEFAULT_BANNER,
    ) -> None:
        self.client = client
        self.banner = banner
        self.terminal = terminal or Terminal.open()
        if border not in ("lines", "box", "borderless"):
            raise ValueError("border must be 'lines', 'box', or 'borderless'")
        if footer not in ("full", "compact", "off"):
            raise ValueError("footer must be 'full', 'compact', or 'off'")
        self.border: Literal["lines", "box", "borderless"] = border
        self.footer_mode: Literal["full", "compact", "off"] = footer
        self.show_session = bool(show_session)
        self._workspace_dir = None if workspace_dir is None else str(workspace_dir)
        self.owns_terminal = terminal is None
        self.session_id = session_id or ""
        self.running = False
        self.styles = Styles(self.terminal.color_system)
        self.composer = Composer(
            styles=self.styles, width=self.terminal.width, style="claude" if border == "lines" else border, gutter="none", padding="compact"
        )
        #: Validated images waiting for the next prompt; every add is bounded.
        self._pending_images = PendingAttachments()
        #: Typed path-entry mode; None means closed, a string is the in-progress path.
        self._attachment_path: str | None = None
        self.key_decoder = KeyDecoder()
        self._escape_flush_at = 0.0
        self.event_queue: queue.Queue[events.TuiFrame] = queue.Queue()
        self._session_cursors: dict[str, int] = {}
        #: Active tools in flight, keyed by invocation id (displayed in Live region).
        self._active_tools: dict[str, ActiveToolProgress] = {}
        #: Last known server identity/liveness from /api/health. The fence bit
        #: pauses dispatch; the identity triple detects a replaced server.
        self.upgrade: HealthStatus | None = None
        #: False until the current generation's stream delivers its first frame;
        #: the stream worker rearms it on every reconnect so each established
        #: stream reconciles exactly once (announced restart or silent drop).
        self._stream_ready = False
        #: Request ids with a worker in flight (approval/confirmation/invoke):
        #: a held Enter/click while one is outstanding is ignored, never a
        #: second thread. Cleared on response, decided fact, or error.
        self._in_flight: set[str] = set()
        #: Approval, active overlay, queued gates, and the suspended live view.
        self._overlays = OverlayStack()
        #: Chord -> descriptor for this catalog revision, rebuilt on every catalog load.
        self._shortcut_bindings: dict[str, CommandDescriptor] = {}
        #: Chord -> claimant names when 2+ descriptors claim one chord (binds nobody).
        self._shortcut_conflicts: dict[str, tuple[str, ...]] = {}
        #: Live read-only input source while a refreshable view is open; only this
        #: explicit metadata enables polling.
        self._live_input: events.LiveInput | None = None
        #: Next monotonic deadline for the live-input refresh worker.
        self._live_refresh_at: float = 0.0
        #: Active plugin live views (`live=True`) projected above the composer.
        self._live_views: tuple[PluginViewDescriptor, ...] = ()
        self._live_views_inflight: bool = False
        self._live_views_refresh_at: float = 0.0
        #: Typed pending gates (the reducer truth) plus decided-id tombstones.
        self._gates = GateState()
        #: Live token buffer keyed by dispatch id: text fragments arrive as
        #: core.provider_progress events and settle on the turn's MODEL_TURN.
        self._stream_buffers: dict[str, list[str]] = {}
        self._stream_reasoning: dict[str, list[str]] = {}
        self._stream_dispatch: str | None = None
        self._spinner_tick = 0
        self._run_active = False
        self._run_started_at: float | None = None
        self._commands_in_flight = 0
        self._quit_armed_at = float("-inf")
        self._cancel_pending = False
        self._queued_followups: tuple[QueuedFollowup, ...] = ()
        self._followup_withdrawing = False
        self.catalog_cache: tuple[CommandDescriptor, ...] = ()
        #: The capability revision the displayed catalog came from; slash dispatch
        #: echoes it verbatim. Rendered overlays carry their own captured revision.
        self.catalog_revision: str = ""
        #: Last catalog acquisition failure; visible until a reload succeeds.
        self._catalog_error: str | None = None
        self._status_line = "Connecting to server..."
        self._status_line_kind = StatusLineKind.NOTICE
        self._stop_event = threading.Event()
        #: Every background operation and lifecycle thread is owned here.
        self._work = WorkGroup()
        self._follower = SessionFollower(
            client=client,
            queue=self.event_queue,
            stop_event=self._stop_event,
            cursors=self._session_cursors,
            on_reconnect=self._note_stream_reconnect,
            work=self._work,
        )
        self._input_stop = threading.Event()
        #: Serializes terminal input-fd reads: the input thread's slices vs kitty
        #: probe/upload queries. Concurrent readers would split reply bytes.
        self._terminal_read_lock = threading.Lock()
        self._input_thread: threading.Thread | None = None
        #: Sessions whose welcome mark already committed; each empty visit shows it once.
        self._welcomed_sessions: set[str] = set()
        #: Slash/path popup state and the one in-flight workspace query.
        self._completion = CompletionController(
            composer=self.composer,
            styles=self.styles,
            work=self._work,
            event_queue=self.event_queue,
            client=self.client,
        )
        self.latest_input_tokens: int | None = None
        self.cumulative_input_tokens: int = 0
        self.cumulative_output_tokens: int = 0
        self._latest_tool_artifacts: tuple[str, tuple[ArtifactSource, ...]] | None = None
        self._artifact_exports = tempfile.TemporaryDirectory(prefix="voidcode-artifact-")
        self._artifact_ack_deadline: tuple[str, str, float] | None = None

    @property
    def status_line(self) -> str:
        return self._status_line

    @status_line.setter
    def status_line(self, value: str) -> None:
        self._status_line = value
        self._status_line_kind = StatusLineKind.NOTICE

    def set_loading_status(self, text: str) -> None:
        self._status_line = text
        self._status_line_kind = StatusLineKind.LOADING

    def clear_loading_status(self) -> None:
        if self._status_line_kind is StatusLineKind.LOADING:
            self.status_line = ""

    def set_followup_queued_status(self, text: str) -> None:
        self._status_line = text
        self._status_line_kind = StatusLineKind.FOLLOWUP_QUEUED

    def clear_followup_queued_status(self) -> None:
        if self._status_line_kind is StatusLineKind.FOLLOWUP_QUEUED:
            self.status_line = ""

    @property
    def _generation(self) -> int:
        """Session epoch, owned by the follower (read-only here)."""
        return self._follower.epoch

    @property
    def _stream_thread(self) -> threading.Thread | None:
        """Current stream thread, owned by the follower (read-only here)."""
        return self._follower.thread

    @property
    def _stream_cancel(self) -> StreamCancellation | None:
        """Current stream cancel handle, owned by the follower (read-only here)."""
        return self._follower.cancel_handle

    @property
    def active_overlay(self) -> events.InteractiveOverlay | None:
        """Facade read view: the overlay stack's focused overlay."""
        return self._overlays.active

    @property
    def pending_approval(self) -> InteractiveApproval | None:
        """Facade read view: the native approval gate above every other overlay."""
        return self._overlays.approval

    @property
    def running(self) -> bool:
        return self._running

    @running.setter
    def running(self, value: bool) -> None:
        self._running = value
        if not value:
            # Wake a main loop blocked in queue.get so tests/quit paths stopping
            # externally never hang; the drain ignores the wake frame. Guarded
            # for __init__ order (running is assigned before the queue exists).
            pending = getattr(self, "event_queue", None)
            if pending is not None:
                pending.put_nowait(events.WakeEvent())

    def _apply_path_completion(self, frame: events.PathCompletionEvent, rows: list[str]) -> None:
        """Apply only the newest reply for the unchanged draft; never clear the draft."""
        self._completion.apply(frame, rows)

    def _sync_slash_completion(self, *, dismissed: bool = False) -> None:
        """Refresh the slash-name popup or queue the workspace path query; preserve selection."""
        self._completion.sync(
            dismissed=dismissed,
            catalog=CompletionCatalog(revision=self.catalog_revision, commands=self.catalog_cache),
            ensure_catalog=self._ensure_completion_catalog,
            session_id=self.session_id,
            generation=self._generation,
        )

    def _ensure_completion_catalog(self) -> CompletionCatalog:
        """Report once when completion is the first visible catalog-dependent action."""
        catalog.get(self)
        if not self.catalog_revision:
            catalog.unavailable(self)
        return CompletionCatalog(revision=self.catalog_revision, commands=self.catalog_cache)

    _path_request = staticmethod(CompletionController.path_request)

    def _render_frame(self) -> None:
        render_frame(self)


def run_tui(
    server_url: str,
    *,
    token: str | None = None,
    session_id: str | None = None,
    border: Literal["lines", "box", "borderless"] = "lines",
    footer: Literal["full", "compact", "off"] = "full",
    show_session: bool = False,
    banner: BannerConfig | None = _DEFAULT_BANNER,
) -> int:
    """Launch the generic TUI connected to a running core."""
    from voidcode.bootstrap import open_session_client

    with open_session_client(server_url=server_url, token=token) as client:
        app = CoreTuiApp(client, session_id=session_id, border=border, footer=footer, show_session=show_session, banner=banner)
        return app.run()


def run_embedded_tui(
    workspace: object,
    state_dir: object,
    *,
    session_id: str | None = None,
    border: Literal["lines", "box", "borderless"] = "lines",
    footer: Literal["full", "compact", "off"] = "full",
    show_session: bool = False,
    banner: BannerConfig | None = _DEFAULT_BANNER,
) -> int:
    """Launch the TUI against an in-process core: no socket, no HTTP framing."""
    import signal

    from voidcode.bootstrap import open_session_client

    workspace_path = Path(str(workspace))
    state_path = None if state_dir is None else Path(str(state_dir))
    with open_session_client(workspace=workspace_path, state_dir=state_path) as client:
        app = CoreTuiApp(
            client,
            session_id=session_id,
            border=border,
            footer=footer,
            show_session=show_session,
            workspace_dir=workspace_path,
            banner=banner,
        )
        active = app

        def _cancel(*_: object) -> None:
            if active.session_id:
                try:
                    client.cancel(active.session_id)
                except Exception:
                    pass

        previous = {number: signal.signal(number, lambda *_: _cancel()) for number in (signal.SIGINT, signal.SIGTERM)}
        try:
            return app.run()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
