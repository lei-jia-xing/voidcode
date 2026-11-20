"""Interactive inline TUI application for the VoidCode core transport."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import tempfile
import threading
import time
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from voidcode.config import BannerConfig
from voidcode.sdk.client import (
    MAX_PROMPT_BYTES,
    MAX_PROMPT_CHARS,
    CommandCatalog,
    CommandResult,
    CoreClientError,
    HealthStatus,
    ResumeSpec,
    RunSpec,
    SessionClient,
    SessionRow,
    StreamCancellation,
)
from voidcode.sdk.codec import decode_tool_call
from voidcode.sdk.command_help import command_argument_hints
from voidcode.sdk.commands import CommandDescriptor
from voidcode.sdk.provider import ProviderTokenUsage, ProviderTurnResult
from voidcode.sdk.tools import (
    ToolEffect,
    ToolResult,
)
from voidcode.sdk.transcript import (
    TranscriptTool,
)
from voidcode.sdk.views import (
    ChoiceOption,
    ChoicesViewData,
    ConfirmationViewData,
    FormViewData,
    MarkdownViewData,
    PluginViewDescriptor,
    ViewsSnapshot,
    is_read_only_load,
    parse_view_data,
)
from voidcode.tui.gates import (
    ActivationCommitted,
    ApprovalGate,
    ApprovalRequested,
    ConfirmationRequested,
    GateChange,
    GateDecided,
    GateState,
    RunTerminated,
    reduce_gates,
)

from . import catalog, events, operations
from .artifact_viewer import ArtifactSource, InteractiveArtifactViewer, artifact_sources, png_dimensions, read_artifact_bytes
from .composer import Composer, ComposerAction
from .event_pump import drain_events
from .formatting import (
    command_result_rows as _command_result_rows,
)
from .formatting import (
    levels_text as _levels_text,
)
from .formatting import (
    model_segment as _model_segment,
)
from .formatting import (
    normalize_chord as _normalize_chord,
)
from .formatting import (
    prefixed_rows as _prefixed_rows,
)
from .formatting import (
    safe as _safe,
)
from .formatting import (
    shortcut_bindings as _shortcut_bindings,
)
from .formatting import (
    slash_display as _slash_display,
)
from .formatting import (
    slash_public_path as _slash_public_path,
)
from .help_view import TUI_CONTROLS, TUI_CONTROLS_BY_NAME, render_help
from .keys import Key, KeyDecoder
from .rendering import ActiveToolProgress, render_frame
from .rendering import banner_rows as _banner_rows
from .session_follower import SessionFollower
from .styles import Styles
from .term import Terminal, TerminalCapabilityError, copy_to_clipboard
from .views import (
    InteractiveApproval,
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    InteractiveMarkdown,
    render_assistant_markdown,
    render_plugin_view,
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

#: Second Ctrl+C inside this window quits; help text uses the same value.
_QUIT_ARM_SECONDS = 1.5

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

_BUILTIN_BANNER = (
    "███╗      ███╗",
    "╚███╗    ███╔╝",
    " ╚███╗  ███╔╝",
    "  ╚███╗███╔╝",
    "   ╚█████╔╝",
    "    ╚███╔╝",
    "     ╚═╝",
)

#: Immutable ctor/entry default: None at the boundary always means disabled.
_DEFAULT_BANNER = BannerConfig()


def _approval_gate_from_payload(payload: Mapping[str, object], *, run_id: str, revision_id: str) -> ApprovalGate:
    """Decode one core approval request payload into its typed gate (outer only)."""
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("approval payload requires a non-empty request_id")
    raw_call = payload.get("call")
    if not isinstance(raw_call, Mapping):
        raise ValueError("approval payload requires a call object")
    call = decode_tool_call(raw_call)
    return ApprovalGate(call=call, request_id=request_id, run_id=run_id, revision_id=revision_id)


_IGNORED_PATH_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".codegraph"}


class CoreTuiApp:
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
        self.active_overlay: (
            InteractiveChoices | InteractiveConfirmation | InteractiveForm | InteractiveMarkdown | InteractiveArtifactViewer | None
        ) = None
        #: Chord -> descriptor for this catalog revision, rebuilt on every catalog load.
        self._shortcut_bindings: dict[str, CommandDescriptor] = {}
        #: Chord -> claimant names when 2+ descriptors claim one chord (binds nobody).
        self._shortcut_conflicts: dict[str, tuple[str, ...]] = {}
        #: Live read-only input source while a refreshable view is open; only this
        #: explicit metadata enables polling: {name, arguments, revision, source}.
        self._live_input: dict[str, Any] | None = None
        #: Next monotonic deadline for the live-input refresh worker.
        self._live_refresh_at: float = 0.0
        #: Retained live view while a confirmation overlays it: (overlay, live_input).
        self._suspended_live: tuple[Any, dict[str, Any] | None] | None = None
        # Native tool approval (core permission gate) is not a plugin Action
        # confirmation: it resumes the parked run with its own request id.
        self.pending_approval: InteractiveApproval | None = None
        #: Confirmation requests that arrived while another overlay owned the live
        #: region, keyed by request_id in request order: none is ever dropped.
        self.queued_confirmations: dict[str, InteractiveConfirmation] = {}
        #: Typed pending gates (the reducer truth) plus decided-id tombstones.
        self._gates = GateState()
        #: Live token buffer keyed by dispatch id: text fragments arrive as
        #: core.provider_progress events and settle on the turn's MODEL_TURN.
        self._stream_buffers: dict[str, list[str]] = {}
        self._stream_reasoning: dict[str, list[str]] = {}
        self._stream_dispatch: str | None = None
        self._spinner_tick = 0
        self._run_active = False
        self._quit_armed_at = float("-inf")
        self._cancel_pending = False
        self.catalog_cache: tuple[CommandDescriptor, ...] = ()
        #: The capability revision the displayed catalog came from; slash dispatch
        #: echoes it verbatim. Rendered overlays carry their own captured revision.
        self.catalog_revision: str = ""
        #: Last catalog acquisition failure; visible until a reload succeeds.
        self._catalog_error: str | None = None
        self.status_line = "Connecting to server..."
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
        self._path_replacements: dict[str, str] = {}
        self._pending_path_replacements: dict[str, str] = {}
        self._pending_path_descriptions: dict[str, str] = {}
        self.latest_input_tokens: int | None = None
        self.cumulative_input_tokens: int = 0
        self.cumulative_output_tokens: int = 0
        self._latest_tool_artifacts: tuple[str, tuple[ArtifactSource, ...]] | None = None
        self._artifact_exports = tempfile.TemporaryDirectory(prefix="voidcode-artifact-")
        self._artifact_ack_deadline: tuple[str, str, float] | None = None

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

    def run(self) -> int:
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
            try:
                if self._stream_cancel is not None:
                    self._stream_cancel.close()
            finally:
                self._work.shutdown()
                self._artifact_exports.cleanup()
                if self.owns_terminal:
                    self.terminal.close()

    def _init_session(self) -> None:
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

    def _maybe_welcome(self) -> None:
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

    def _start_event_stream(self) -> None:
        """Begin following the current session; the follower owns thread + cancel lifecycle."""
        self._follower.start_stream(self.session_id)

    def _note_stream_reconnect(self) -> None:
        """Clear stream readiness; the follower calls this on every reconnect."""
        self._stream_ready = False

    def _main_loop(self) -> int:
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

    def _start_input_thread(self) -> None:
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

    def _animations_live(self) -> bool:
        return bool(self._run_active or self._stream_dispatch is not None or self._active_tools)

    def _live_refresh_armed(self) -> bool:
        """Whether a live refresh would fire now apart from its deadline (mirror of _poll_live_input's gates)."""
        live = self._live_input
        if live is None or self._live_refresh_at <= 0.0:
            return False
        if self.pending_approval is not None or not isinstance(self.active_overlay, (InteractiveChoices, InteractiveMarkdown)):
            return False
        return getattr(self.active_overlay, "revision_id", "") == str(live.get("revision", ""))

    def _idle_timeout(self) -> float:
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
        if self._artifact_ack_deadline is not None:
            consider(self._artifact_ack_deadline[2])
        if self._quit_armed_at != float("-inf"):
            consider(self._quit_armed_at + _QUIT_ARM_SECONDS)
        if self._animations_live():
            consider(now + _SPINNER_TICK_SECONDS)
        # Fully idle: a long liveness snooze, not a spin. Real events, input,
        # and the shutdown wake return from queue.get immediately; renders
        # still happen only on drained frames, fired timers, or animations.
        return min(best, _MAX_IDLE_BLOCK_SECONDS) if best is not None else _MAX_IDLE_BLOCK_SECONDS

    def _fire_due_timers(self) -> bool:
        """Run due timer actions; returns True when visible state may have changed."""
        now = time.monotonic()
        fired = False
        self._poll_live_input()
        if self._handle_input_bytes(b""):
            fired = True
        if self.terminal.resize_pending():
            # A native resize arrived mid-park: force the render that consumes it.
            fired = True
        if self._quit_armed_at != float("-inf") and now >= self._quit_armed_at + _QUIT_ARM_SECONDS:
            self._quit_armed_at = float("-inf")
            fired = True
        deadline = self._artifact_ack_deadline
        if deadline is not None and now >= deadline[2]:
            fired = True
        return fired

    def _poll_live_input(self) -> None:
        """Fire one live refresh when its deadline passes and the view still owns focus."""
        live = self._live_input
        if live is None or self._live_refresh_at <= 0.0:
            return
        if time.monotonic() < self._live_refresh_at:
            return
        if self.pending_approval is not None or not isinstance(self.active_overlay, (InteractiveChoices, InteractiveMarkdown)):
            return  # gate/editor owns focus: pause, keep the deadline so resume fires at once
        if getattr(self.active_overlay, "revision_id", "") != str(live.get("revision", "")):
            self._live_input = None
            self._live_refresh_at = 0.0
            return
        self._live_refresh_at = time.monotonic() + 60.0  # rearmed by the reply; never stacks workers
        self._refresh_live_input_async()

    def _live_stream_rows(self, width: int) -> list[str]:
        """Partial markdown of the in-flight dispatch for the live region, or nothing."""
        dispatch = self._stream_dispatch
        if dispatch is None:
            return []
        text = "".join(self._stream_buffers.get(dispatch, []))
        if not text:
            return []
        return render_assistant_markdown(text, width, self.styles)

    def _drain_events(self, timeout: float | None = None) -> int:
        """Drain typed frames through the one pump/router in ``event_pump``."""
        return drain_events(self, timeout)

    def _record_model_turn(self, payload: Mapping[str, object], entry_content: object = None) -> None:
        usage = None
        if isinstance(entry_content, ProviderTurnResult) and entry_content.usage is not None:
            usage = entry_content.usage
        if usage is None:
            raw_result = payload.get("result")
            if isinstance(raw_result, ProviderTurnResult) and raw_result.usage is not None:
                usage = raw_result.usage
            elif isinstance(raw_result, Mapping):
                usage = raw_result.get("usage")
        if usage is None:
            usage = payload.get("usage")

        in_tok: int | None = None
        out_tok: int | None = None
        if isinstance(usage, ProviderTokenUsage):
            if not isinstance(usage.input_tokens, bool) and isinstance(usage.input_tokens, int):
                in_tok = usage.input_tokens
            if not isinstance(usage.output_tokens, bool) and isinstance(usage.output_tokens, int):
                out_tok = usage.output_tokens
        elif isinstance(usage, Mapping):
            raw_in = usage.get("input_tokens")
            if not isinstance(raw_in, bool) and isinstance(raw_in, int):
                in_tok = raw_in
            raw_out = usage.get("output_tokens")
            if not isinstance(raw_out, bool) and isinstance(raw_out, int):
                out_tok = raw_out

        if in_tok is not None:
            self.latest_input_tokens = in_tok
            self.cumulative_input_tokens += in_tok
        else:
            self.latest_input_tokens = None
        if out_tok is not None:
            self.cumulative_output_tokens += out_tok

    def _record_tool_artifact(self, target: TranscriptTool | ToolResult) -> None:
        if isinstance(target, TranscriptTool):
            tool_name = target.call.tool_name or "tool"
            result = target.result
        else:
            tool_name = target.tool_name or "tool"
            result = target
        sources = artifact_sources(result)
        if sources:
            self._latest_tool_artifacts = (tool_name, sources)

    def _show_truncated_output(self) -> None:
        if self._latest_tool_artifacts is None:
            self.terminal.commit_rows(
                [
                    self.styles.fg(
                        "warning",
                        "No immutable tool artifact is available; inline content is not a full-source fallback.",
                    )
                ]
            )
            return
        title, sources = self._latest_tool_artifacts
        self._close_live_input()
        viewer = InteractiveArtifactViewer(
            sources, revision_id=catalog.displayed_revision(self), session_id=self.session_id, title=f"Tool artifacts: {title}"
        )
        self.active_overlay = viewer
        self._load_artifact_page(viewer, 0)

    def _artifact_context(self, viewer: InteractiveArtifactViewer) -> dict[str, Any]:
        return {"viewer": viewer.identity, "session_id": self.session_id, "generation": self._generation}

    def _artifact_cancelled(self, viewer: InteractiveArtifactViewer, sha256: str) -> bool:
        return (
            self._stop_event.is_set() or self.active_overlay is not viewer or viewer.session_id != self.session_id or viewer.source.sha256 != sha256
        )

    def _load_artifact_page(self, viewer: InteractiveArtifactViewer, offset: int) -> None:
        request_id = viewer.begin_loading(offset)
        sha256 = viewer.source.sha256
        context = self._artifact_context(viewer)

        def worker() -> None:
            try:
                page = self.client.read_artifact(sha256, offset=offset)
                self.event_queue.put(events.ArtifactPageEvent(**context, request_id=request_id, page=page))
            except Exception as exc:
                self.event_queue.put(events.ArtifactErrorEvent(**context, request_id=request_id, error=str(exc)))

        self._work.submit("ArtifactPage", worker)

    def _load_artifact_image(self, viewer: InteractiveArtifactViewer) -> None:
        if not viewer.is_png or viewer.image_status != "unavailable":
            return
        sha256 = viewer.source.sha256
        cached = self.terminal.cached_kitty_image(sha256)
        if cached is not None:
            viewer.mark_image_cached(sha256, cached)
            return
        with self._terminal_read_lock:
            graphics_supported = self.terminal.kitty_graphics_supported
        if not graphics_supported:
            viewer.fail_image(sha256, "Kitty Graphics probe not confirmed; O explicitly opens a fetched original copy.")
            return
        if self.terminal.cell_pixel_size is None:
            viewer.fail_image(sha256, "Terminal did not report pixel/cell geometry; O explicitly opens a fetched original copy.")
            return
        request_id = viewer.begin_image_load()
        context = self._artifact_context(viewer)

        def worker() -> None:
            try:
                data = read_artifact_bytes(
                    self.client,
                    sha256,
                    progress=lambda received: self.event_queue.put(
                        events.ArtifactImageProgressEvent(**context, request_id=request_id, sha256=sha256, received=received)
                    ),
                    cancelled=lambda: self._artifact_cancelled(viewer, sha256),
                )
                dimensions = png_dimensions(data)
                if dimensions is None:
                    raise ValueError("PNG artifact has no valid IHDR geometry")
                self.event_queue.put(events.ArtifactImageEvent(**context, request_id=request_id, sha256=sha256, data=data))
                if self._artifact_cancelled(viewer, sha256):
                    return
                # Probe under the lock (it reads the reply); transmit outside it:
                # holding the lock across a bulk PTY write would starve input.
                with self._terminal_read_lock:
                    supported = self.terminal.kitty_graphics_supported
                if not supported:
                    raise TerminalCapabilityError("Kitty Graphics is not supported by the active terminal")
                self.terminal.upload_kitty_png(sha256, data, dimensions)
                self.event_queue.put(events.ArtifactTransmittedEvent(**context, request_id=request_id, sha256=sha256))
            except InterruptedError:
                return
            except Exception as exc:
                self.event_queue.put(events.ArtifactImageErrorEvent(**context, request_id=request_id, sha256=sha256, error=str(exc)))

        self._work.submit("ArtifactImage", worker)

    def _open_artifact(self, viewer: InteractiveArtifactViewer) -> None:
        request_id = viewer.begin_open()
        sha256 = viewer.source.sha256
        media_type = viewer.media_type.split(";", 1)[0]
        context = self._artifact_context(viewer)

        def worker() -> None:
            path: Path | None = None
            try:
                data = read_artifact_bytes(self.client, sha256, cancelled=lambda: self._artifact_cancelled(viewer, sha256))
                suffix = mimetypes.guess_extension(media_type) or ".bin"
                fd, filename = tempfile.mkstemp(prefix=f"{sha256}-", suffix=suffix, dir=self._artifact_exports.name)
                path = Path(filename)
                with os.fdopen(fd, "wb") as target:
                    target.write(data)
                if self._artifact_cancelled(viewer, sha256):
                    path.unlink(missing_ok=True)
                    return
                if not webbrowser.open_new_tab(path.as_uri()):
                    raise RuntimeError("No external viewer accepted the immutable temporary copy")
                message = f"Opened original copy: {path.as_uri()} (removed when this TUI closes)"
            except InterruptedError:
                return
            except Exception as exc:
                if path is not None:
                    path.unlink(missing_ok=True)
                message = f"External open failed: {exc}; source remains artifact://{sha256}"
            self.event_queue.put(events.ArtifactOpenEvent(**context, request_id=request_id, message=message))

        self._work.submit("ArtifactOpen", worker)

    def _render_run_notice(self, status: str, detail: object, rows: list[str], *, stop_cause: object = None) -> None:
        if status == "failed":
            rows.extend(_prefixed_rows("Error:", str(detail or "Run failed"), self.styles, slot="error"))
        elif stop_cause in ("user_stop", "owner_cancel"):
            rows.append(self.styles.fg("dim", "Operation aborted"))
        elif detail:
            label = "Stopped:" if stop_cause == "limit_reached" else "Interrupted:"
            rows.extend(_prefixed_rows(label, str(detail), self.styles, slot="dim"))
        else:
            rows.append(self.styles.fg("dim", "Interrupted"))

    def _apply_gate_change(self, change: GateChange) -> None:
        """Fold one typed gate change and sync the mounted widgets to the reducer."""
        before = self._gates
        after = reduce_gates(before, change)
        self._gates = after
        if isinstance(change, ApprovalRequested):
            if after.approval is not None and after.approval.request_id == change.gate.request_id:
                current = self.pending_approval
                if current is None or current.request_id != change.gate.request_id:
                    self.pending_approval = InteractiveApproval(change.gate)
                self.status_line = f"Tool approval required: {change.gate.tool_name}"
            return
        if isinstance(change, ConfirmationRequested):
            gate = change.gate
            if gate.request_id in after.decided or not any(entry.request_id == gate.request_id for entry in after.confirmations):
                return
            self._show_or_queue_confirmation(InteractiveConfirmation(gate.view, revision_id=gate.revision_id))
            return
        if isinstance(change, GateDecided):
            decided = change.request_id
            self.queued_confirmations.pop(decided, None)
            current = self.pending_approval
            if current is not None and current.request_id == decided:
                self.pending_approval = None
            overlay = self.active_overlay
            if isinstance(overlay, InteractiveConfirmation) and overlay.request_id == decided:
                self.active_overlay = None
                self._present_queued_confirmation()
            return
        if isinstance(change, ActivationCommitted):
            self.pending_approval = None
            for queued_id in [i for i, q in self.queued_confirmations.items() if q.revision_id != change.to_revision_id]:
                del self.queued_confirmations[queued_id]
            if self._suspended_live is not None and getattr(self._suspended_live[0], "revision_id", "") != change.to_revision_id:
                self._suspended_live = None
                self._live_input = None
                self._live_refresh_at = 0.0
            if before.approval is not None or len(before.confirmations) != len(after.confirmations):
                self._present_queued_confirmation()
            return
        if isinstance(change, RunTerminated):
            current = self.pending_approval
            if current is not None and after.approval is None:
                self.pending_approval = None
                self._present_queued_confirmation()
            return
        raise ValueError(f"unknown gate change: {type(change).__name__}")

    def _settle_stream(self, payload: Mapping[str, object]) -> None:
        """Drop the live buffer the committed turn replaces; the rows settle, never replay."""
        dispatch = payload.get("dispatch_id")
        if isinstance(dispatch, str) and dispatch:
            self._stream_buffers.pop(dispatch, None)
            self._stream_reasoning.pop(dispatch, None)
            if self._stream_dispatch == dispatch:
                self._stream_dispatch = None
        else:
            self._stream_buffers.clear()
            self._stream_reasoning.clear()
            self._stream_dispatch = None

    def _clear_run_tools(self, session_id: str, run_id: str | None = None) -> None:
        to_remove = [
            inv_id
            for inv_id, tool in self._active_tools.items()
            if tool.session_id == session_id and (run_id is None or tool.run_id is None or tool.run_id == run_id)
        ]
        for inv_id in to_remove:
            self._active_tools.pop(inv_id, None)

    def _clear_stream(self) -> None:
        self._stream_buffers.clear()
        self._stream_reasoning.clear()
        self._stream_dispatch = None
        if not self._active_tools:
            self._run_active = False
        self._cancel_pending = False
        self.status_line = ""

    def _close_live_input(self) -> None:
        """Stop polling; the open overlay (if any) is the caller's to clear."""
        self._live_input = None
        self._live_refresh_at = 0.0
        self._follower.abort_live()
        self._suspended_live = None

    def _restore_suspended_live(self) -> None:
        """Bring back the live view a direct confirmation decision just closed."""
        suspended = self._suspended_live
        self._suspended_live = None
        if suspended is None:
            return
        overlay, live = suspended
        if self.active_overlay is not None or self.pending_approval is not None:
            return
        revision = getattr(overlay, "revision_id", "")
        if not revision or revision != self.catalog_revision:
            return
        self.active_overlay = overlay
        self._live_input = live
        if live is not None:
            self._live_refresh_at = time.monotonic()
        self.status_line = ""

    def _show_or_queue_confirmation(self, overlay: InteractiveConfirmation) -> None:
        """Show one pending confirmation, or retain it until the live region frees.

        The stored request id is the identity: the same request arriving again as a
        duplicate fact or as its live pending result never resets the modal, and a
        decided request never comes back.
        """
        request_id = overlay.request_id
        if not request_id or request_id in self._gates.decided:
            return
        current = self.active_overlay
        if isinstance(current, InteractiveConfirmation) and current.request_id == request_id:
            return
        if current is None and self.pending_approval is None:
            self.active_overlay = overlay
            self.status_line = "Confirmation required"
            return
        if isinstance(current, (InteractiveChoices, InteractiveMarkdown)) and self._live_input is not None and self._suspended_live is None:
            # A live view yields to its own cancel confirmation: the instance
            # (filter/selection/scroll) is retained, polling pauses, and only a
            # direct decision restores it — never a historical replay.
            self._suspended_live = (current, self._live_input)
            self._live_refresh_at = 0.0
            self._follower.abort_live()
            self.active_overlay = overlay
            self.status_line = "Confirmation required"
            return
        self.queued_confirmations.setdefault(request_id, overlay)

    def _present_queued_confirmation(self) -> None:
        """Present the oldest still-pending confirmation once the live region is free."""
        if self.active_overlay is not None or self.pending_approval is not None:
            return
        while self.queued_confirmations:
            request_id, overlay = next(iter(self.queued_confirmations.items()))
            del self.queued_confirmations[request_id]
            if request_id in self._gates.decided:
                continue
            self.active_overlay = overlay
            self.status_line = "Confirmation required"
            return
        if self._suspended_live is not None:
            self._restore_suspended_live()

    def _apply_kitty_reply(self, reply: Any) -> None:
        viewer = self.active_overlay
        if isinstance(viewer, InteractiveArtifactViewer) and viewer.source.sha256 == reply.sha256:
            if reply.message != "OK":
                viewer.fail_image(reply.sha256, f"Kitty {reply.operation} rejected image: {reply.message}")
            elif reply.operation == "upload":
                viewer.mark_image_uploaded(reply.sha256)
            if (
                reply.operation == "upload"
                and self._artifact_ack_deadline is not None
                and self._artifact_ack_deadline[:2] == (viewer.identity, reply.sha256)
            ):
                # The awaited acknowledgement arrived (success or failure): release the deadline.
                self._artifact_ack_deadline = None

    def _handle_input_bytes(self, chunk: bytes, *, arrived_at: float | None = None) -> bool:
        """Decode bytes and dispatch keys; empty input only flushes a due escape. Returns True if any key dispatched."""
        now = time.monotonic()
        stamp = arrived_at if arrived_at is not None else now
        handled = False
        if chunk and self._escape_flush_at and stamp >= self._escape_flush_at:
            # Backlogged chunk past the window: resolve the pending escape first
            # so it can never merge into unrelated later bytes.
            self._escape_flush_at = 0.0
            flushed = self.key_decoder.flush()
            handled = bool(flushed)
            if self._dispatch_keys(flushed):
                return True
            now = time.monotonic()
        if chunk:
            # A lone ESC is ambiguous until no further bytes follow: the standard
            # disambiguation window resolves it as Esc.
            self._escape_flush_at = stamp + _ESCAPE_FLUSH_SECONDS
            keys = list(self.key_decoder.feed(chunk))
        elif self._escape_flush_at and now >= self._escape_flush_at:
            self._escape_flush_at = 0.0
            keys = self.key_decoder.flush()
        else:
            keys = []
        if self._dispatch_keys(keys):
            return True
        return handled or bool(keys)

    def _dispatch_keys(self, keys: list[Key]) -> bool:
        """Dispatch decoded keys; returns True when quitting (caller must stop)."""
        for key in keys:
            if key.name == "escape" and (self.pending_approval is not None or self.active_overlay is not None):
                # The decoder names the raw byte "escape"; the overlays speak
                # "esc". The composer keeps the decoder's own name.
                key = Key("esc")
            if key.name == "ctrl+c":
                now = time.monotonic()
                if now - self._quit_armed_at < _QUIT_ARM_SECONDS:
                    self.running = False
                    return True
                self._quit_armed_at = now
                self.composer.clear()
                continue
            self._quit_armed_at = float("-inf")
            if key.name == "ctrl+d" and not self.composer.value and self.pending_approval is None and self.active_overlay is None:
                self.running = False
                return True
            if self.pending_approval is not None:
                self._handle_approval_key(key)
            elif self.active_overlay is not None:
                self._handle_overlay_key(key)
            else:
                self._handle_composer_key(key)
        return False

    def _cancel_active_run(self) -> None:
        if not self._run_active or self._cancel_pending:
            return
        self._cancel_pending = True
        self.status_line = ""
        session_id, generation = self.session_id, self._generation

        def worker() -> None:
            try:
                res = self.client.cancel(session_id)
                if not res.cancellation_requested:
                    self.event_queue.put(events.CancelDoneEvent(session_id=session_id, generation=generation))
            except Exception as exc:
                self._queue_notice(f"Cancel error: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.CancelDoneEvent(session_id=session_id, generation=generation))

        self._work.submit("CoreCancel", worker)

    def _handle_approval_key(self, key: Any) -> None:
        """Native approval gate: scroll the preview, a=allow, d/Esc=deny; both hit /resume."""
        assert self.pending_approval is not None
        outcome = self.pending_approval.handle_key(key)
        if outcome is None:
            return
        self._resolve_approval(str(outcome.get("decision", "deny")))

    def _resolve_approval(self, decision: str) -> None:
        if decision not in ("allow", "deny"):
            self.status_line = f"Unknown approval decision: {_safe(decision)}"
            return
        assert decision == "allow" or decision == "deny"
        approval = self.pending_approval
        if approval is None:
            return
        request_id = approval.request_id
        if not request_id:
            self.status_line = "No pending approval to resolve"
            return
        if request_id in self._in_flight:
            return  # a held key while the decision is outstanding: not a second thread
        # The gate stays mounted until the server accepts or a durable decided
        # fact closes it: clearing early would strand the approval, because
        # applied-cursor dedup never replays the old request fact.
        self._in_flight.add(request_id)
        session_id, generation = self.session_id, self._generation
        self.status_line = f"Submitting approval {decision}..."

        def worker() -> None:
            try:
                self.client.resume(session_id, ResumeSpec(approval_request_id=request_id, decision=decision))
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    # Still armed with the same request id: no auto-resend, the
                    # user re-decides after the restart.
                    self._upgrade_pending_notice(f"approval {decision}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"Approval {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"Approval {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            self.event_queue.put(events.GateAcceptedEvent(request_id=request_id, session_id=session_id, generation=generation))

        self._work.submit("CoreApproval", worker)

    def _handle_overlay_key(self, key: Any) -> None:
        assert self.active_overlay is not None
        outcome = self.active_overlay.handle_key(key)
        if outcome is None:
            return

        if outcome.get("cancel"):
            self._follower.abort_interaction()
            self._close_live_input()
            self.active_overlay = None
            self._artifact_ack_deadline = None
            self.status_line = ""
            self._present_queued_confirmation()
            return
        if isinstance(self.active_overlay, InteractiveArtifactViewer):
            if outcome.get("action") == "page":
                self._load_artifact_page(self.active_overlay, int(outcome["offset"]))
            elif outcome.get("action") == "open":
                self._open_artifact(self.active_overlay)
            return

        if isinstance(self.active_overlay, InteractiveConfirmation):
            self._resolve_confirmation(str(outcome.get("decision", "deny")))
        elif isinstance(self.active_overlay, (InteractiveChoices, InteractiveForm, InteractiveMarkdown)):
            action = str(outcome.get("action", ""))
            arguments = outcome.get("arguments", {})
            if isinstance(self.active_overlay, InteractiveChoices) and action == "tui.switch_session":
                self.active_overlay = None
                target_id = str(arguments.get("session_id", "")) if isinstance(arguments, dict) else ""
                if target_id and target_id != self.session_id:
                    self._switch_session(target_id)
                return
            revision = self.active_overlay.revision_id
            if outcome.get("prepare_input"):
                descriptors = catalog.get(self, retry=True)
                if not self.catalog_revision:
                    catalog.unavailable(self)
                    return
                descriptor = next((item for item in descriptors if item.name == action), None)
                if revision != self.catalog_revision or descriptor is None or not descriptor.has_input:
                    self.status_line = "Command input unavailable; refresh the catalog"
                    return
                live_arguments = arguments if isinstance(arguments, dict) else {}
                if self._live_input is not None:
                    self._live_input = {"name": action, "arguments": dict(live_arguments), "revision": revision, "source": self.active_overlay}
                self.active_overlay = None
                self._prepare_command_input_async(descriptor, arguments=live_arguments)
                return
            # The overlay (with its typed inputs) stays mounted until the
            # server accepts: a late 503 must not lose the user's draft. The
            # in-flight marker stops a held Enter from spawning a second call.
            submit_key = f"invoke:{action}@{revision}"
            if submit_key in self._in_flight:
                return
            self._in_flight.add(submit_key)
            self.status_line = f"Dispatching /{_slash_display(action)}..."
            self._invoke_async(action, arguments if isinstance(arguments, dict) else {}, revision, submit_key=submit_key)

    def _resolve_confirmation(self, decision: str) -> None:
        """Submit the stored request id's decision; the overlay never lies about it."""
        if decision not in ("allow", "deny"):
            self.status_line = f"Unknown confirmation decision: {_safe(decision)}"
            return
        assert decision == "allow" or decision == "deny"
        request_id = getattr(self.active_overlay, "request_id", "")
        # The revision of the envelope that displayed this confirmation.
        revision = getattr(self.active_overlay, "revision_id", "")
        if not request_id:
            self.status_line = "No pending confirmation to resolve"
            return
        if request_id in self._in_flight:
            return  # a held key while the decision is outstanding: not a second thread
        # The overlay stays mounted until the server accepts or the durable
        # decided fact closes it: closing early would strand the request,
        # because applied-cursor dedup never replays the old requested fact.
        self._in_flight.add(request_id)
        session_id, generation = self.session_id, self._generation
        self.status_line = f"Submitting confirmation {decision}..."

        def worker() -> None:
            try:
                self.client.confirm(
                    session_id,
                    request_id,
                    decision,
                    expected_revision=revision,
                )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    # Still armed with the same request id and revision: no
                    # auto-resend, the user re-decides after the restart.
                    self._upgrade_pending_notice(f"confirmation {decision}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"Confirmation {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"Confirmation {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put(events.DecisionDoneEvent(request_id=request_id, session_id=session_id, generation=generation))
                return
            self.event_queue.put(events.GateAcceptedEvent(request_id=request_id, session_id=session_id, generation=generation))

        self._work.submit("CoreConfirm", worker)

    def _invoke_async(
        self,
        name: str,
        arguments: dict[str, Any],
        revision: str,
        *,
        submit_key: str | None = None,
        surface_result: bool = False,
        display_name: str | None = None,
    ) -> None:
        """Run one command off the UI thread under the revision the user saw.

        Only a core read-only receipt is surfaced directly. Plugin commands
        project their result through the durable ``core.command_completed``
        event, and run/resume project through run events; rendering those direct
        answers too would duplicate settled rows. ``surface_result`` comes from
        catalog metadata (owner plus effects), never a client command list.
        """
        token = self._follower.begin_interaction()
        submitted_overlay = self.active_overlay
        shown = display_name if display_name else _slash_display(name)
        session_id, generation = self.session_id, self._generation

        def finish(status: str, result: CommandResult | None = None) -> None:
            """Hand this command's captured completion to the UI thread once."""
            self.event_queue.put(events.CommandDoneEvent(name=name, status=status, result=result, session_id=session_id, generation=generation))

        def worker() -> None:
            try:
                res = self.client.invoke_command(
                    session_id,
                    name,
                    arguments,
                    expected_revision=revision,
                )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    self._upgrade_pending_notice(f"/{shown}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"/{shown} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                    finish("error")
                if submit_key is not None:
                    self.event_queue.put(events.DecisionDoneEvent(request_id=submit_key, session_id=session_id, generation=generation))
                return
            except Exception as exc:
                self._queue_notice(f"/{shown} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                finish("error")
                if submit_key is not None:
                    self.event_queue.put(events.DecisionDoneEvent(request_id=submit_key, session_id=session_id, generation=generation))
                return
            status = res.status
            if status == "pending_confirmation":
                confirmation = res.confirmation
                if confirmation is not None:
                    self.event_queue.put(
                        events.ConfirmationEvent(confirmation=confirmation, revision_id=revision, session_id=session_id, generation=generation)
                    )
            elif status == "error":
                self._queue_notice(
                    f"/{shown} failed: {res.error or 'error'}",
                    tone="error",
                    session_id=session_id,
                    generation=generation,
                )
            # An error notice already owns the failure text; a plugin/run result
            # is already projected by its durable event. Only the core read
            # receipt has no other display path.
            receipt = res if surface_result and status == "ok" else None
            finish(status, receipt)
            # Failed validation leaves the editor and draft mounted for correction.
            # Only an accepted action may close the overlay that submitted it.
            if submit_key is not None:
                if status == "error":
                    self.event_queue.put(events.DecisionDoneEvent(request_id=submit_key, session_id=session_id, generation=generation))
                else:
                    self.event_queue.put(
                        events.InvokeAcceptedEvent(
                            request_id=submit_key,
                            action=name,
                            revision_id=revision,
                            overlay=submitted_overlay,
                            session_id=session_id,
                            generation=generation,
                        )
                    )

            if res.status == "ok" and res.presentation is not None:
                presentation = res.presentation
                self.event_queue.put(
                    events.CommandPresentationEvent(
                        token=token,
                        view=PluginViewDescriptor(
                            owner="command", name=name, title=presentation.title, view_type=presentation.view_type, data=presentation.data
                        ),
                        revision_id=revision,
                        session_id=session_id,
                        generation=generation,
                    )
                )

        self._work.submit("CoreInvoke", worker)

    def _queue_notice(
        self,
        text: str,
        *,
        tone: str = "info",
        session_id: str | None = None,
        generation: int | None = None,
    ) -> None:
        """Hand worker-side text to the UI thread; only the main loop commits rows."""
        self.event_queue.put(
            events.NoticeEvent(
                text=text,
                tone=tone,
                session_id=self.session_id if session_id is None else session_id,
                generation=self._generation if generation is None else generation,
            )
        )

    def _upgrade_pending_notice(self, action: str, *, session_id: str, generation: int) -> None:
        """Name a fenced refusal and refresh identity so the status line shows the fence."""
        self._refresh_health_async(session_id=session_id, generation=generation)
        self._queue_notice(
            f"Core is draining for an upgrade; {action} was not dispatched",
            tone="warning",
            session_id=session_id,
            generation=generation,
        )

    def _refresh_health_async(self, *, session_id: str | None = None, generation: int | None = None) -> None:
        """Refresh server identity/liveness off the UI thread; stale replies are dropped."""
        self._follower.refresh_health(
            self.session_id if session_id is None else session_id,
            self._generation if generation is None else generation,
        )

    def _reconcile_connection_async(self) -> None:
        """ONE connection-established reconciliation path, entirely off the UI thread.

        Runs on the first frame of every (re)connected stream — announced restart
        or silent drop. Fetches health + catalog + durable session status in one
        worker round, then hands the UI thread token-tagged envelopes.
        Optional views are never fetched here: only explicit ``/tui views``
        presents them. Only the newest round renders (generation alone cannot
        order overlapping replies). Never resends the prompt, never touches the
        applied cursor.
        """
        self._follower.reconcile_connection(self.session_id)

    def _handle_composer_key(self, key: Any) -> None:
        if self.status_line.startswith("Loading "):
            self.status_line = ""
        self._follower.abort_interaction()
        name = key.name
        chord = _normalize_chord(name)
        if chord in self._shortcut_bindings:
            # A catalog chord: never through the composer, never as slash text.
            self._dispatch_shortcut(self._shortcut_bindings[chord])
            return
        if chord in self._shortcut_conflicts:
            claimants = ", ".join(f"/{_slash_display(entry)}" for entry in self._shortcut_conflicts[chord])
            self.terminal.commit_rows([self.styles.fg("warning", f"Shortcut {name} is claimed by {claimants}; no action bound")])
            return
        if not self.catalog_revision and "+" in chord and chord != "ctrl+o" and not self.composer.owns_key(chord):
            # A modified chord the composer does not own can only be a catalog
            # shortcut; with no catalog it must say so instead of doing nothing.
            catalog.get(self, retry=True)
            return
        if chord == "ctrl+o":
            self._show_truncated_output()
            return
        if name == "enter" and not self.composer.completion:
            line = self.composer.value.strip()
            if line and not line.startswith("/"):
                nchars = len(line)
                nbytes = len(line.encode("utf-8"))
                if nchars > MAX_PROMPT_CHARS or nbytes > MAX_PROMPT_BYTES:
                    self.status_line = (
                        f"Prompt too large ({nchars} chars, {nbytes} bytes; "
                        f"max {MAX_PROMPT_CHARS} chars, {MAX_PROMPT_BYTES} bytes) — draft kept, nothing sent"
                    )
                    return
        if name in ("enter", "tab") and self.composer.completion:
            selected = self.composer.completion[self.composer.completion_index]
            if selected in self._path_replacements:
                replacement = self._path_replacements[selected]
                self.composer.accept_completion_with(replacement)
                self._sync_slash_completion()
                return
            paths = [f"/{_slash_public_path(entry)}" for entry in self.catalog_cache if entry.slash_visible]
            paths.extend(f"/tui {control.name}" for control in TUI_CONTROLS)
            is_group = selected not in paths
            if is_group or (name == "tab" and any(path.startswith(selected + " ") for path in paths)):
                self.composer.accept_completion_with(selected + " ")
                self._sync_slash_completion()
                return
        if name == "enter" and self.composer.completion and self.composer.value == self.composer.completion[self.composer.completion_index]:
            self.composer.set_completion(())
        elif name in ("enter", "tab", "up", "down", "escape") and (self.composer.completion or name in ("tab", "up", "down", "escape")):
            was_open = bool(self.composer.completion)
            outcome = self.composer.handle_key(key)
            if name not in ("up", "down") or not self.composer.completion:
                self._sync_slash_completion(dismissed=(was_open and name in ("enter", "tab")) or name == "escape")
            return self._outcome(outcome)
        outcome = self.composer.handle_key(key)
        self._sync_slash_completion()
        return self._outcome(outcome)

    def _outcome(self, outcome: Any) -> None:
        if outcome.action == ComposerAction.CANCEL_TURN:
            self._cancel_active_run()
        elif outcome.action == ComposerAction.INTERRUPT:
            self.running = False
        elif outcome.action == ComposerAction.COPY and outcome.text is not None:
            try:
                copy_to_clipboard(outcome.text)
            except ValueError as exc:
                self.status_line = f"Copy failed: {exc} — draft kept"
            except OSError as exc:
                self.status_line = f"Copy failed: {exc}"
            else:
                nbytes = len(outcome.text.encode("utf-8"))
                self.status_line = f"Copied {nbytes} bytes to clipboard"
        elif outcome.action == ComposerAction.SUBMIT and outcome.text is not None:
            line = outcome.text.strip()
            if not line:
                return
            if line.startswith("/"):
                if self._dispatch_slash_command(line) is False:
                    self.composer.set_text(outcome.text)
                return
            catalog.get(self, retry=True)
            if not self.catalog_revision:
                catalog.unavailable(self)
                self.composer.set_text(outcome.text)
                return
            client_syntax = self._client_syntax_command(line)
            if client_syntax is not None:
                descriptor, argument = client_syntax
                self._dispatch_catalog_command(descriptor, argument)
                return
            self._dispatch_user_prompt(line)

    def _client_syntax_command(self, line: str) -> tuple[CommandDescriptor, str] | None:
        """Match a non-slash catalog prefix (``!`` for shell), never a client table."""
        descriptors = catalog.get(self)
        if not self.catalog_revision:
            catalog.unavailable(self)
            return None
        for descriptor in descriptors:
            syntax = descriptor.client_syntax
            if syntax and not syntax.startswith("/") and line.startswith(syntax):
                return descriptor, line[len(syntax) :].lstrip()
        return None

    @property
    def _workspace_root(self) -> Path:
        if self._workspace_dir is not None:
            return Path(self._workspace_dir).resolve()
        if self.upgrade is not None and self.upgrade.workspace:
            return Path(self.upgrade.workspace).resolve()
        return Path.cwd().resolve()

    def _path_candidates(self, query: str) -> tuple[list[str], dict[str, str], dict[str, str]]:
        root = self._workspace_root
        try:
            if not root.is_dir():
                return [], {}, {}
        except OSError:
            return [], {}, {}

        if "/" in query:
            dir_part, _, file_part = query.rpartition("/")
            clean_dir = dir_part.strip("/")
            target_dir = (root / clean_dir).resolve() if clean_dir else root
            try:
                target_dir.relative_to(root)
                rel_parts = target_dir.relative_to(root).parts
                if any(part in _IGNORED_PATH_DIRS for part in rel_parts):
                    return [], {}, {}
            except ValueError:
                return [], {}, {}
            prefix = f"{clean_dir}/" if clean_dir else ""
        else:
            file_part = query
            target_dir = root
            prefix = ""

        try:
            if not target_dir.is_dir():
                return [], {}, {}
            entries = list(target_dir.iterdir())
        except OSError:
            return [], {}, {}

        allow_hidden = query.startswith(".") or file_part.startswith(".")
        matching_dirs: list[str] = []
        matching_files: list[str] = []
        descriptions: dict[str, str] = {}
        replacements: dict[str, str] = {}

        for entry in entries:
            name = entry.name
            if name in _IGNORED_PATH_DIRS:
                continue
            if not allow_hidden and name.startswith("."):
                continue
            if file_part and not name.lower().startswith(file_part.lower()):
                continue

            try:
                is_dir = entry.is_dir()
            except OSError:
                continue

            rel_path = f"{prefix}{name}"
            if is_dir:
                cand = f"{rel_path}/"
                matching_dirs.append(cand)
                descriptions[cand] = "directory"
                replacements[cand] = f"{rel_path}/"
            else:
                cand = rel_path
                matching_files.append(cand)
                descriptions[cand] = "file"
                replacements[cand] = f"{rel_path} "

        matching_dirs.sort(key=lambda s: s.lower())
        matching_files.sort(key=lambda s: s.lower())
        candidates = (matching_dirs + matching_files)[:50]
        return candidates, descriptions, replacements

    def _sync_slash_completion(self, *, dismissed: bool = False) -> None:
        """Refresh the slash-name or path popup after one composer key; preserve selection."""
        if dismissed:
            self.composer.set_completion(())
            self._path_replacements = {}
            self._pending_path_replacements = {}
            self._pending_path_descriptions = {}
            return
        candidates, token_range = self._completion_candidates()
        if not candidates or token_range is None:
            self.composer.set_completion(())
            self._path_replacements = {}
            self._pending_path_replacements = {}
            self._pending_path_descriptions = {}
            return
        previous = self.composer.completion
        index = self.composer.completion_index
        selected = previous[index] if previous and 0 <= index < len(previous) else None
        self.composer.set_completion(
            candidates,
            index=candidates.index(selected) if selected in candidates else 0,
            target_range=token_range,
        )
        if self._pending_path_replacements:
            self._path_replacements = dict(self._pending_path_replacements)
            self.composer.completion_descriptions = dict(self._pending_path_descriptions)
            self.composer.completion_groups = set()
        else:
            self._path_replacements = {}
            descriptions: dict[str, str] = {}
            for entry in self.catalog_cache:
                path = f"/{_slash_public_path(entry)}"
                hints = " ".join(command_argument_hints(entry))
                descriptions[path] = f"{hints} — {entry.description}" if hints else entry.description
            self.composer.completion_descriptions = descriptions
            self.composer.completion_groups = {
                candidate
                for candidate in candidates
                if any(f"/{_slash_public_path(entry)}".startswith(candidate + " ") for entry in self.catalog_cache if entry.slash_visible)
                or candidate == "/tui"
            }

    def _completion_candidates(self) -> tuple[list[str], tuple[int, int] | None]:
        """Slash-command or workspace @path paths matching the draft token at cursor."""
        text = self.composer.value
        cursor = self.composer.cursor

        at_idx = text[:cursor].rfind("@")
        if at_idx >= 0 and (at_idx == 0 or text[at_idx - 1] in " \t\n([{\"':"):
            token_mid = text[at_idx:cursor]
            if not any(c in " \t\n" for c in token_mid):
                token_end = cursor
                while token_end < len(text) and text[token_end] not in " \t\n)]}\"':":
                    token_end += 1
                query = text[at_idx + 1 : cursor]
                candidates, descriptions, replacements = self._path_candidates(query)
                self._pending_path_descriptions = descriptions
                self._pending_path_replacements = replacements
                return candidates, (at_idx, token_end)

        slash_idx = text[:cursor].rfind("/")
        if slash_idx > 0 and text[slash_idx - 1] not in " \t\n":
            path_start = cursor
            while path_start > 0 and text[path_start - 1] not in " \t\n([{\"':":
                path_start -= 1
            path_token = text[path_start:cursor]
            if "/" in path_token and "\n" not in path_token:
                token_end = cursor
                while token_end < len(text) and text[token_end] not in " \t\n)]}\"':":
                    token_end += 1
                candidates, descriptions, replacements = self._path_candidates(path_token)
                if candidates:
                    self._pending_path_descriptions = descriptions
                    self._pending_path_replacements = replacements
                    return candidates, (path_start, token_end)

        self._pending_path_descriptions = {}
        self._pending_path_replacements = {}

        if slash_idx < 0:
            return [], None
        if slash_idx > 0 and text[slash_idx - 1] not in " \t\n":
            return [], None
        prefix = text[slash_idx + 1 : cursor]
        if "\n" in prefix:
            return [], None

        token_end = cursor
        while token_end < len(text) and (text[token_end].isalnum() or text[token_end] in "_-"):
            token_end += 1

        if not self.catalog_revision:
            # Completion is the first visible hint that commands are unavailable;
            # report once (no forced retry) and keep the typed draft untouched.
            catalog.get(self)
            if not self.catalog_revision:
                catalog.unavailable(self)
        names = [_slash_public_path(entry) for entry in self.catalog_cache if entry.slash_visible]
        names.extend(f"tui {control.name}" for control in TUI_CONTROLS)
        parent, _, partial = prefix.rpartition(" ")
        base = f"{parent} " if parent else ""
        candidates = {base + name[len(base) :].split(" ", 1)[0] for name in names if name.startswith(base) and name[len(base) :].startswith(partial)}
        candidate_list = sorted(f"/{name}" for name in candidates if name and name != parent)
        if not candidate_list:
            return [], None
        return candidate_list, (slash_idx, token_end)

    def _dispatch_user_prompt(self, prompt: str) -> None:
        # No optimistic commit: the durable core.user_message event is the one
        # and only source of the prompt row on screen.
        if self.upgrade is not None and self.upgrade.fenced:
            # Never queue a prompt behind a fence: the user re-sends after
            # reconnect. Composer._submit already cleared the draft, so restore
            # the exact text — but never overwrite a newer draft the user typed
            # while this key was being handled.
            if not self.composer.value.strip():
                self.composer.set_text(prompt)
            self.status_line = "Core is draining for an upgrade; retry after it restarts"
            return
        if self._run_active:
            session_id, generation = self.session_id, self._generation

            def followup_worker() -> None:
                try:
                    res = self.client.run(session_id, RunSpec(prompt))
                    if res.status == "queued":
                        self.event_queue.put(events.FollowupQueuedEvent(session_id=session_id, generation=generation))
                    elif res.status in ("failed", "interrupted") and res.error:
                        self.event_queue.put(events.FollowupFailedEvent(prompt=prompt, error=res.error, session_id=session_id, generation=generation))
                    else:
                        self.event_queue.put(
                            events.RunStatusEvent(
                                status=res.status,
                                error=res.error,
                                run_id=res.run_id,
                                session_id=session_id,
                                generation=generation,
                            )
                        )
                except Exception as exc:
                    self.event_queue.put(events.FollowupFailedEvent(prompt=prompt, error=str(exc), session_id=session_id, generation=generation))

            self._work.submit("CoreFollowup", followup_worker)
            return
        self.status_line = ""
        self._run_active = True
        session_id, generation = self.session_id, self._generation

        def run_worker() -> None:
            try:
                res = self.client.run(session_id, RunSpec(prompt))
                status = res.status
                self.event_queue.put(
                    events.RunStatusEvent(
                        status=status,
                        error=res.error,
                        run_id=res.run_id,
                        session_id=session_id,
                        generation=generation,
                    )
                )
                if status == "waiting" and res.pending_approval is not None:
                    try:
                        gate = _approval_gate_from_payload(res.pending_approval, run_id=res.run_id, revision_id=res.revision_id)
                    except ValueError as exc:
                        self._queue_notice(f"Approval receipt malformed: {exc}", tone="error", session_id=session_id, generation=generation)
                    else:
                        self.event_queue.put(events.RunWaitingEvent(gate=gate, session_id=session_id, generation=generation))
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    self.event_queue.put(events.RunRejectedEvent(prompt=prompt, session_id=session_id, generation=generation))
                    self._upgrade_pending_notice("the prompt", session_id=session_id, generation=generation)
                else:
                    self.event_queue.put(events.RunStatusEvent(status="failed", error=str(exc), session_id=session_id, generation=generation))
            except Exception as exc:
                self.event_queue.put(events.RunStatusEvent(status="failed", error=str(exc), session_id=session_id, generation=generation))

        self._work.submit("CoreRun", run_worker)

    def _dispatch_slash_command(self, line: str) -> bool:
        """Dispatch one slash line; False means the draft must be restored."""
        rest = line[1:].strip()
        if not rest:
            return True
        head, _, arg_str = rest.partition(" ")

        # "/tui <control>" always reaches the client's own controls, so a plugin
        # that declares e.g. "help" cannot make local navigation unreachable.
        if head == "tui":
            local, _, local_args = arg_str.strip().partition(" ")
            if not local:
                self._show_help()
                return True
            if self._run_local_control(local, local_args.strip()):
                return True
            self.terminal.commit_rows([self.styles.fg("error", f"Unknown TUI control: /tui {_safe(local)}")])
            return True

        descriptors = catalog.get(self, retry=True)
        if not self.catalog_revision:
            catalog.unavailable(self)
            return False
        tokens = rest.split()
        ranked = sorted(
            (
                (len(parts), index, entry)
                for index, entry in enumerate(descriptors)
                if (parts := _slash_public_path(entry).split()) and parts == tokens[: len(parts)]
            ),
            key=lambda item: (item[0], -item[1]),
            reverse=True,
        )
        if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            width, _, entry = ranked[0]
            tail = rest.split(maxsplit=width)[width] if len(tokens) > width else ""
            self._dispatch_catalog_command(entry, tail)
            return True
        if ranked:
            self.terminal.commit_rows([f"{self.styles.fg('error', f'Unknown command: /{_safe(rest)}')} (try /help)"])
            return True

        # A bare name is a local control only when no admitted command owns it.
        if head not in {c.name for c in descriptors} and self._run_local_control(head, arg_str):
            return True
        self._dispatch_catalog_command(None, rest)
        return True

    def _run_local_control(self, cmd: str, arg_str: str) -> bool:
        """Run one client-local control; False when the name is not a control."""
        control = TUI_CONTROLS_BY_NAME.get(cmd)
        if control is None:
            return False
        name = control.name
        if name == "help":
            self._show_help()
        elif name == "output":
            self._show_truncated_output()
        elif name == "sessions":
            self._show_sessions()
        elif name == "session":
            if arg_str:
                self._switch_session(arg_str.strip())
        elif name == "new":
            self._create_session()
        elif name == "cancel":
            self._cancel_active_run()
        elif name == "views":
            self._show_views(arg_str)
        elif name == "model":
            self._tui_model(arg_str.strip())
        elif name == "thinking":
            self._tui_thinking(arg_str.strip())
        elif name == "quit":
            self.running = False
        return True

    def _action_schema(self, name: str) -> Mapping[str, Any] | None:
        """The target command's declared argument schema, the form's field authority."""
        for descriptor in catalog.get(self, retry=True):
            if descriptor.name == name:
                return descriptor.argument_schema
        if not self.catalog_revision:
            catalog.unavailable(self)
        return None

    def _switch_session(self, session_id: str) -> None:
        """Rebind the follower to another session.

        Each session keeps its own committed cursor, so A -> B -> A replays
        nothing that was already settled in A; in-flight results from the
        previous session are dropped by the generation/session guard.
        """
        if isinstance(self.active_overlay, InteractiveArtifactViewer):
            self.terminal.commit_rows([self.styles.fg("warning", f"Artifact viewer closed on session switch: {self.active_overlay.source.uri}")])
        self._artifact_ack_deadline = None
        self._follower.new_epoch()
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                break
        self.session_id = session_id
        self._close_live_input()
        self.active_overlay = None
        self.queued_confirmations.clear()
        self._gates = GateState()
        self._active_tools.clear()
        self._clear_stream()
        self.latest_input_tokens = None
        self.cumulative_input_tokens = 0
        self.cumulative_output_tokens = 0
        self._latest_tool_artifacts = None
        # A new stream is about to start: its first frame reconciles.
        self._stream_ready = False
        self.status_line = ""
        self._maybe_welcome()
        self._start_event_stream()

    def _command_receipt_rows(self, name: str, result: CommandResult) -> list[str]:
        """Render one command receipt; core help uses the declarative help screen."""
        if name == "help":
            help_rows = self._help_receipt_rows(result)
            if help_rows is not None:
                return help_rows
        return _command_result_rows(name, result, self.styles, self.terminal.width)

    def _help_receipt_rows(self, result: CommandResult) -> list[str] | None:
        """Render a core help receipt from its catalog data, or None to fall back.

        The core ``help`` command is the only producer of a full catalog payload
        in a receipt. The TUI never invents commands from ``output`` text; if an
        older/foreign server sends only text, the generic receipt renderer wins.
        """
        if result.status != "ok" or result.data is None:
            return None
        try:
            catalog = CommandCatalog.from_payload(result.data)
        except ValueError:
            return None
        bindings, conflicts = _shortcut_bindings(catalog.commands)
        return render_help(
            catalog.commands,
            revision=catalog.revision_id,
            width=self.terminal.content_width,
            styles=self.styles,
            bindings=bindings,
            conflicts=conflicts,
            quit_arm_seconds=_QUIT_ARM_SECONDS,
        )

    def _show_help(self) -> None:
        """Commit the client help screen; catalog failure degrades to local controls."""
        catalog.get(self)
        catalog_error = self._catalog_error
        if catalog_error is not None:
            catalog.unavailable(self)
        self.terminal.commit_rows(
            render_help(
                self.catalog_cache,
                revision=self.catalog_revision,
                width=self.terminal.content_width,
                styles=self.styles,
                bindings=self._shortcut_bindings,
                conflicts=self._shortcut_conflicts,
                quit_arm_seconds=_QUIT_ARM_SECONDS,
                catalog_error=catalog_error,
            )
        )

    def _tui_model(self, arg: str) -> None:
        """Show the admitted model detail, or switch it (embedded, idle only)."""
        model = getattr(self.upgrade, "model", None)
        if not arg:
            self.terminal.commit_rows(
                [
                    self.styles.render("accent", "=== Model (admitted) ==="),
                    f"  model: {_safe(_model_segment(model))}",
                    f"  thinking: {_safe(getattr(model, 'reasoning_effort', None))}",
                    f"  supported levels: {_safe(_levels_text(model))}",
                ]
            )
            return
        self._tui_switch_model("model", arg)

    def _tui_thinking(self, arg: str) -> None:
        """Show current + supported levels, or set one (embedded, idle only)."""
        model = getattr(self.upgrade, "model", None)
        if not arg:
            self.terminal.commit_rows(
                [
                    self.styles.render("accent", "=== Thinking (admitted) ==="),
                    f"  current: {_safe(getattr(model, 'reasoning_effort', None))}",
                    f"  supported levels: {_safe(_levels_text(model))}",
                ]
            )
            return
        if arg.lstrip("+-").isdigit():
            self.terminal.commit_rows([self.styles.fg("error", "Numeric budgets are hidden here: use a supported named level or off")])
            return
        self._tui_switch_model("reasoning_effort", arg)

    def _tui_switch_model(self, field: Literal["model", "reasoning_effort"], value: str) -> None:
        """Rewrite the workspace model section and reload once, entirely off the UI thread."""
        from voidcode.tui.local_client import CoreLocalClient

        if not isinstance(self.client, CoreLocalClient):
            self.terminal.commit_rows([self.styles.fg("error", "Model config lives with the server: switch from the server workspace")])
            return
        if self._run_active or self.pending_approval is not None or isinstance(self.active_overlay, InteractiveConfirmation):
            self.terminal.commit_rows([self.styles.fg("error", "Session is busy: resolve the run or gate first")])
            return
        if field == "model":
            if not value.strip():
                self.terminal.commit_rows([self.styles.fg("error", "Model must be a non-empty provider/model string")])
                return
            new_value: str | int = value
        else:
            new_value = value
            levels = getattr(getattr(self.upgrade, "model", None), "reasoning_levels", None)
            if isinstance(levels, (list, tuple)) and levels and new_value != "off" and new_value not in levels:
                supported = _levels_text(getattr(self.upgrade, "model", None))
                self.terminal.commit_rows([self.styles.fg("error", f"Unsupported level for this model: {_safe(new_value)} ({_safe(supported)})")])
                return
        if self._workspace_dir is None:
            self.terminal.commit_rows([self.styles.fg("error", "No workspace: cannot rewrite configuration")])
            return
        if not self.catalog_revision:
            catalog.get(self, retry=True)
            if not self.catalog_revision:
                catalog.unavailable(self)
                return
        workspace = Path(self._workspace_dir)
        revision = self.catalog_revision
        session_id, generation = self.session_id, self._generation
        client = self.client
        self.status_line = f"Switching {field}..."
        kind = field

        self._work.submit(
            "ModelSwitch",
            lambda: self.event_queue.put(
                operations.switch_model(
                    client,
                    workspace=workspace,
                    field=kind,
                    value=new_value,
                    revision=revision,
                    session_id=session_id,
                    generation=generation,
                )
            ),
        )

    def _show_sessions(self) -> None:
        """List sessions off the UI thread; stale results never replace a newer binding."""
        session_id, generation = self.session_id, self._generation
        self.status_line = "Loading sessions..."
        client = self.client

        self._work.submit(
            "CoreSessions",
            lambda: self.event_queue.put(operations.list_sessions(client, session_id=session_id, generation=generation)),
        )

    def _present_sessions(self, sessions: list[SessionRow]) -> None:
        if not sessions:
            self.terminal.commit_rows([self.styles.fg("warning", "No active sessions.")])
            return
        options = tuple(
            ChoiceOption(
                id=row.session_id,
                label=f"{row.session_id[:8]} (turn {row.turn}, {row.status})" + (" [current]" if row.session_id == self.session_id else ""),
            )
            for row in sessions
        )
        data = ChoicesViewData(options=options, action="tui.switch_session", argument_name="session_id")
        overlay = InteractiveChoices(data, revision_id=catalog.displayed_revision(self), title="Active Sessions")
        for idx, row in enumerate(sessions):
            if row.session_id == self.session_id:
                overlay.selected_index = idx
                break
        self.active_overlay = overlay

    def _create_session(self) -> None:
        """Create a session off the UI thread, then switch only if still on the same binding."""
        session_id, generation = self.session_id, self._generation
        self.status_line = "Creating session..."
        client = self.client

        self._work.submit(
            "CoreCreateSession",
            lambda: self.event_queue.put(operations.create_session(client, session_id=session_id, generation=generation)),
        )

    def _show_views(self, name_filter: str = "") -> None:
        """Read plugin views off the UI thread; only explicit requests present them.

        Passive views are committed inline. Interactive choices/forms/view-
        confirmations are snapshots, not pending durable requests, so this is
        the one path allowed to mount one; true approval and confirmation gates
        are restored separately from their durable events.
        """
        self._follower.abort_interaction()
        session_id, generation = self.session_id, self._generation
        self.status_line = "Loading views..."
        client = self.client

        self._work.submit(
            "CoreViews",
            lambda: self.event_queue.put(operations.load_views(client, session_id=session_id, generation=generation, name_filter=name_filter)),
        )

    def _present_views(self, snapshot: ViewsSnapshot, name_filter: str) -> None:
        views_revision = snapshot.revision_id
        descriptors: list[PluginViewDescriptor] = list(snapshot.views)
        if name_filter:
            wanted = name_filter.strip().lower()
            descriptors = [v for v in descriptors if wanted in v.name.lower() or wanted in v.title.lower()]
        if not descriptors:
            self.terminal.commit_rows([self.styles.fg("warning", "No plugin views available for this session.")])
            return
        for v in descriptors:
            if self._mount_interactive_view(v, views_revision):
                return
            # Non-interactive view: render rows and commit
            rows = render_plugin_view(v, self.terminal.width, self.styles)
            self.terminal.commit_rows(rows)

    def _mount_interactive_view(self, view: PluginViewDescriptor, revision: str) -> bool:
        """Mount an explicitly requested view with its captured revision."""
        data = parse_view_data(view.view_type, view.data)
        if isinstance(data, MarkdownViewData):
            if not data.actions:
                return False
            self.active_overlay = InteractiveMarkdown(data, revision_id=revision, title=view.title)
        elif isinstance(data, ChoicesViewData):
            self.active_overlay = InteractiveChoices(data, revision_id=revision, title=view.title)
        elif isinstance(data, FormViewData):
            form = InteractiveForm(data, action_schema=self._action_schema(data.action), revision_id=revision, title=view.title)
            self._maybe_load_form_data(form)
            self.active_overlay = form
        elif isinstance(data, ConfirmationViewData):
            self.active_overlay = InteractiveConfirmation(data, revision_id=revision)
        else:
            return False
        return True

    def _maybe_load_form_data(self, form: InteractiveForm) -> None:
        """Run the declared load_action off the UI thread, exactly when it is read-only."""
        if not form.load_action:
            return
        load_action = str(form.load_action)
        commands = catalog.get(self, retry=True)
        if not self.catalog_revision:
            catalog.unavailable(self)
            form.load_error = f"Catalog unavailable: {_safe(self._catalog_error or 'unknown error')} (retry)"
            return
        catalog_snapshot = CommandCatalog(revision_id=self.catalog_revision, commands=commands)
        if not is_read_only_load(form.view, catalog_snapshot):
            form.load_error = f"load_action '{load_action}' must declare exactly the read effect"
            return

        session_id, generation = self.session_id, self._generation
        target = form

        def worker() -> None:
            data: Mapping[str, object] | None = None
            error: str | None = None
            try:
                res = self.client.invoke_command(
                    session_id,
                    load_action,
                    expected_revision=target.revision_id,
                )
                if isinstance(res.data, Mapping):
                    data = res.data
                elif res.error:
                    error = str(res.error)
            except Exception as exc:
                error = str(exc)
            self.event_queue.put(events.FormLoadEvent(form=target, data=data, error=error, session_id=session_id, generation=generation))

        self._work.submit("CoreFormLoad", worker)

    def _prepare_command_input_async(self, descriptor: CommandDescriptor, *, arguments: Mapping[str, Any] | None = None) -> None:
        """Read optional plugin input off the UI thread without invoking its action."""
        token = self._follower.begin_interaction()
        session_id, generation, revision = self.session_id, self._generation, self.catalog_revision
        owned_arguments = dict(arguments or {})
        # Hidden actions (Alt+A monitor) show their human description, never a
        # raw /dotted id a user could mistake for a slash command.
        self.status_line = f"Loading {_safe(descriptor.description) if not descriptor.slash_visible else '/' + _slash_public_path(descriptor)}..."

        self._work.submit(
            "CoreCommandInput",
            lambda: self.event_queue.put(
                operations.command_input(
                    self.client,
                    owner=descriptor.owner,
                    name=descriptor.name,
                    revision=revision,
                    arguments=owned_arguments,
                    token=token,
                    session_id=session_id,
                    generation=generation,
                )
            ),
        )

    def _refresh_live_input_async(self) -> None:
        """Re-read the open live view's source once; stale replies never render."""
        live = self._live_input
        if live is None:
            return
        token = self._follower.begin_live()
        session_id, generation = self.session_id, self._generation
        action = str(live.get("name", ""))
        revision = str(live.get("revision", ""))
        owned_arguments = dict(live.get("arguments") or {})

        self._work.submit(
            "CoreLiveInput",
            lambda: self.event_queue.put(
                operations.live_input(
                    self.client,
                    action=action,
                    revision=revision,
                    arguments=owned_arguments,
                    token=token,
                    session_id=session_id,
                    generation=generation,
                )
            ),
        )

    def _arm_live_input(self, *, name: str, arguments: Mapping[str, Any], revision: str, refresh_after_ms: int | None, source: Any) -> None:
        """Enable polling only from explicit refresh metadata on the open view."""
        if refresh_after_ms is None:
            self._live_input = None
            self._live_refresh_at = 0.0
            return
        self._live_input = {
            "name": name,
            "arguments": dict(arguments),
            "revision": revision,
            "source": source,
            "refresh_after_ms": refresh_after_ms,
        }
        self._live_refresh_at = time.monotonic() + refresh_after_ms / 1000.0

    def _mount_live_view(
        self, view: PluginViewDescriptor, revision: str, *, name: str, arguments: Mapping[str, Any], refresh_after_ms: int | None
    ) -> bool:
        """Mount one live view and arm its refresh; empty choices stay open."""
        if not self._mount_interactive_view(view, revision):
            return False
        self._suspended_live = None
        self._arm_live_input(name=name, arguments=arguments, revision=revision, refresh_after_ms=refresh_after_ms, source=self.active_overlay)
        return True

    def _dispatch_shortcut(self, descriptor: CommandDescriptor) -> None:
        """Run one cached catalog chord without touching the composer draft."""
        if self.upgrade is not None and self.upgrade.fenced:
            self.status_line = "Core is draining for an upgrade; retry after it restarts"
            return
        if descriptor.has_input:
            self._prepare_command_input_async(descriptor)
            return
        self._dispatch_catalog_command(descriptor, "")

    def _dispatch_catalog_command(self, cmd_desc: CommandDescriptor | None, arg_str: str) -> None:
        if cmd_desc is None:
            self.terminal.commit_rows([f"{self.styles.fg('error', f'Unknown command: /{_safe(arg_str)}')} (try /help)"])
            return
        # The revision of the catalog this descriptor was read from.
        revision = self.catalog_revision
        if not arg_str and cmd_desc.has_input:
            self._prepare_command_input_async(cmd_desc)
            return
        # Core read commands answer through the session client; plugin commands and run/resume
        # already project through durable events. Classify by contract metadata,
        # never by a client-side command name list.
        effects = frozenset(cmd_desc.effects)
        surface_result = cmd_desc.owner == "core" and effects <= {ToolEffect.READ}

        schema = cmd_desc.argument_schema
        raw_properties = schema.get("properties")
        properties: dict[str, object] = (
            {name: value for name, value in raw_properties.items() if isinstance(name, str)} if isinstance(raw_properties, Mapping) else {}
        )
        raw_required = schema.get("required")
        required = tuple(name for name in raw_required if isinstance(name, str)) if isinstance(raw_required, (list, tuple)) else ()
        single_required = properties.get(required[0]) if len(required) == 1 else None

        arguments: dict[str, Any]
        if not arg_str and required and properties:
            from voidcode.sdk.views import FormField, FormFieldType, FormViewData

            field_types: dict[str, FormFieldType] = {"string": "text", "integer": "integer", "number": "number", "boolean": "boolean"}
            field_specs: list[tuple[str, Mapping[str, object], FormFieldType]] = []
            for name, spec in properties.items():
                if not isinstance(spec, Mapping):
                    field_specs.clear()
                    break
                type_name = spec.get("type")
                if not isinstance(type_name, str) or type_name not in field_types:
                    field_specs.clear()
                    break
                field_specs.append((name, spec, field_types[type_name]))
            if properties and len(field_specs) == len(properties):
                fields = tuple(
                    FormField(
                        name=name,
                        label=str(spec.get("description") or name),
                        field_type=field_type,
                        required=name in required,
                    )
                    for name, spec, field_type in field_specs
                )
                self.active_overlay = InteractiveForm(
                    FormViewData(fields=fields, action=cmd_desc.name),
                    action_schema=schema,
                    revision_id=revision,
                    title=cmd_desc.description,
                )
                return
        if not arg_str:
            arguments = {}
        elif cmd_desc.text_argument is not None and not arg_str.lstrip().startswith("{"):
            arguments = {cmd_desc.text_argument: arg_str}
        elif (
            len(properties) == 1
            and len(required) == 1
            and required[0] in properties
            and isinstance(single_required, Mapping)
            and single_required.get("type") == "string"
        ):
            # A single required string schema maps slash trailing text generically.
            arguments = {required[0]: arg_str}
        else:
            try:
                parsed = json.loads(arg_str)
            except json.JSONDecodeError:
                if len(properties) == 1:
                    arguments = {next(iter(properties)): arg_str}
                else:
                    self.terminal.commit_rows([self.styles.fg("error", f"Arguments must be valid JSON: {_safe(arg_str)}")])
                    return
            else:
                if not isinstance(parsed, dict):
                    self.terminal.commit_rows([self.styles.fg("error", f"Arguments must be a JSON object: {_safe(arg_str)}")])
                    return
                arguments = parsed

        self.status_line = f"Dispatching /{_slash_public_path(cmd_desc)}..."
        self._invoke_async(cmd_desc.name, arguments, revision, surface_result=surface_result, display_name=_slash_public_path(cmd_desc))

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
    from pathlib import Path

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
