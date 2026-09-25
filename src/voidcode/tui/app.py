"""Inline TUI: the app loop that owns the terminal and drives the runtime client.

The renderer is split into pure layers (``term`` / ``region`` / ``transcript`` /
``statusline`` / ``composer`` / ``overlay`` / ``events``); this module is the only
place that knows all of them, and the only place that decides *when* a frame is
written.

Contracts implemented here:

* **Commit once.** Rows of settled blocks leave the app exactly once:
  ``Transcript.take_settled()`` -> ``LiveRegion.commit()`` ->
  ``Terminal.commit_rows()`` (native scrollback, never repainted). One exception
  is documented on :meth:`TuiApp._toggle_expand`: expanding a block that was
  already committed has to re-print the settled tape, because inline scrollback
  cannot be rewritten.
* **Paint the tail.** Only the live tail -- unsettled blocks, the status line and
  the composer (or the active overlay) -- enters ``Terminal.paint_frame``.
* **Frame cadence.** At most one frame every ``MIN_RENDER_INTERVAL_MS`` (33 ms),
  with omp's adaptive backoff (``delay >= 2 x last frame cost``, capped at
  ``_MAX_FRAME_SECONDS``) and a deferral while the stream queue is backed up, so a
  slow terminal receives fresh frames instead of every intermediate one.
* **One runtime thread.** The runtime's stream API is a blocking iterator; it is
  drained on a ``threading.Thread`` into a ``queue.Queue``. That is the old
  Textual ``@work(thread=True)`` + ``post_message`` boundary without Textual: the
  loop never blocks on the runtime.
* **Alternate screen only for a fullscreen overlay.** The transcript stays on the
  normal buffer, so terminal scrollback survives; while the alternate buffer is
  borrowed nothing is committed and only the overlay is painted.

No busy-wait: ``Terminal.read_bytes(timeout)`` with a short timeout, shorter while
a stream is live or a frame is pending, longer when idle.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final

from ..runtime.config import RuntimeConfig, load_runtime_config
from ..runtime.contracts import RuntimeRequest, RuntimeStreamChunk
from ..runtime.events import EventEnvelope
from ..runtime.permission import PermissionDecision
from ..runtime.question import QuestionResponse
from ..runtime.service import VoidCodeRuntime
from ..runtime.session_metadata_helpers import session_model_identity
from .composer import Composer, ComposerAction
from .events import (
    ApprovalRequest,
    QuestionRequest,
    SessionView,
    ViewState,
    format_runtime_error,
)
from .keys import Key, KeyDecoder
from .overlay import (
    ApprovalOverlay,
    Overlay,
    OverlayOutcomeKind,
    QuestionOverlay,
    SessionPickerOverlay,
)
from .region import LiveRegion
from .statusline import StatusLine, StatusSegmentData
from .term import Terminal
from .theme import Theme, resolve_theme
from .transcript import (
    AssistantBlock,
    KeyHints,
    NoticeBlock,
    SessionMarkerBlock,
    ToolBlock,
    UserBlock,
    short_session_id,
)

__all__ = ["MIN_RENDER_INTERVAL_MS", "KeyBindingError", "TuiApp", "parse_key_binding", "run_tui"]

logger = logging.getLogger(__name__)

#: omp ``MIN_RENDER_INTERVAL_MS`` (``tui.ts:812``) -- the frame cadence ceiling.
MIN_RENDER_INTERVAL_MS: Final = 1000 / 30
_MIN_FRAME_SECONDS: Final = MIN_RENDER_INTERVAL_MS / 1000
#: omp ``MAX_ADAPTIVE_RENDER_MS`` (``tui.ts:819``): the ~5 fps backoff floor.
_MAX_FRAME_SECONDS: Final = 0.2
#: omp ``OUTPUT_BACKLOG_RETRY_MS`` (``tui.ts:835``).
_BACKLOG_RETRY_SECONDS: Final = 0.01
#: Pending stream items above which a frame is deferred (omp defers on pending
#: terminal bytes; our backlog is the event queue).
_WRITE_BACKLOG_LIMIT: Final = 256
#: Events folded per loop turn, so a flood cannot starve input or frames.
_DRAIN_BATCH: Final = 64
#: A lone ``ESC`` (omp ``app.interrupt``: cancel the turn, dismiss an overlay) is
#: ambiguous until it is known not to start a sequence; ``KeyDecoder`` buffers it
#: and resolves it on ``flush()``. This is the idle window after which the loop
#: resolves whatever is still buffered -- the standard ESC disambiguation delay.
_ESCAPE_FLUSH_SECONDS: Final = 0.05
#: ``read_bytes`` timeout while a stream or a frame is pending.
_ACTIVE_POLL_SECONDS: Final = 0.02
#: ``read_bytes`` timeout when fully idle.
_IDLE_POLL_SECONDS: Final = 0.25
#: The parent-session background-notice poll (the old app's 1 s ``set_timer``).
_BACKGROUND_POLL_SECONDS: Final = 1.0
#: omp ``SPINNER_ADVANCE_MS`` (``loader.ts:7``).
_SPINNER_ADVANCE_SECONDS: Final = 0.08
#: Bounded wait for the stream thread during shutdown (it is a daemon thread).
_SHUTDOWN_JOIN_SECONDS: Final = 2.0
#: ``/expand`` artifact read limit, unchanged from the old app.
_ARTIFACT_READ_LIMIT: Final = 10_000
#: The one slash command the composer completes.
_SLASH_COMMANDS: Final[tuple[str, ...]] = ("/expand",)

_BACKGROUND_TERMINAL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "runtime.background_task_completed",
        "runtime.background_task_failed",
        "runtime.background_task_cancelled",
    }
)

# ---------------------------------------------------------------------------
# Keymap
# ---------------------------------------------------------------------------

#: Actions ``config.tui.keymap`` may bind. Anything else fails loudly: the old
#: Textual ``self.bind()`` silently accepted unknown action names.
_ACTIONS: Final[frozenset[str]] = frozenset({"session_new", "session_resume", "tools_expand"})

#: The only default binding (omp ``app.tools.expand = ctrl+o``). ``session_new``
#: and ``session_resume`` stay unbound unless the user configures them, exactly
#: as in the old app.
_DEFAULT_KEYMAP: Final[Mapping[str, str]] = MappingProxyType({"tools_expand": "ctrl+o"})

#: Canonical modifier order, mirroring ``keys._format_with_mods``.
_KEY_MODIFIERS: Final[tuple[str, ...]] = ("shift", "ctrl", "alt", "super")
#: Spellings a config may use for the canonical names the decoder emits.
_KEY_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "esc": "escape",
        "return": "enter",
        "del": "delete",
        "ins": "insert",
        "pageup": "pageUp",
        "pagedown": "pageDown",
        "pgup": "pageUp",
        "pgdn": "pageDown",
    }
)
#: Named keys the decoder can emit that a binding may name.
_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "escape",
        "enter",
        "tab",
        "space",
        "backspace",
        "insert",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageUp",
        "pageDown",
        "clear",
        *(f"f{number}" for number in range(1, 13)),
    }
)


class KeyBindingError(ValueError):
    """A ``config.tui.keymap`` entry that cannot be bound (unknown key or action)."""


def parse_key_binding(spec: str) -> Key:
    """Parse ``"ctrl+o"`` into the :class:`~voidcode.tui.keys.Key` the decoder emits.

    Accepts a modifier chord (``shift``/``ctrl``/``alt``/``super``), a named key
    (``escape``, ``pageUp``, ``f5``, …) or a single printable character. Raises
    :class:`KeyBindingError` for anything else -- the app never guesses.
    """
    if not isinstance(spec, str):
        raise KeyBindingError(f"key binding must be a string, got {type(spec).__name__}")
    parts = [part.strip().lower() for part in spec.strip().split("+")]
    if not parts or any(part == "" for part in parts):
        raise KeyBindingError(f"invalid key binding: {spec!r}")
    *modifiers, base = parts
    seen: set[str] = set()
    for modifier in modifiers:
        if modifier not in _KEY_MODIFIERS:
            raise KeyBindingError(f"unknown modifier {modifier!r} in key binding {spec!r}")
        if modifier in seen:
            raise KeyBindingError(f"duplicate modifier {modifier!r} in key binding {spec!r}")
        seen.add(modifier)
    name = _KEY_ALIASES.get(base, base)
    if name not in _KEY_NAMES and not (len(name) == 1 and name.isprintable()):
        raise KeyBindingError(f"unknown key {base!r} in key binding {spec!r}")
    ordered = [modifier for modifier in _KEY_MODIFIERS if modifier in seen]
    return Key("+".join([*ordered, name]))


def parse_keymap(keymap: Mapping[str, str] | None) -> dict[str, Key]:
    """Resolve ``config.tui.keymap`` into action -> :class:`Key`, or fail loudly.

    The config maps a key chord to an action (``{"ctrl+o": "tools_expand"}``);
    the resolved inverse is what the loop dispatches on.
    """
    bindings = {action: parse_key_binding(spec) for action, spec in _DEFAULT_KEYMAP.items()}
    for spec, action in (keymap or {}).items():
        if action not in _ACTIONS:
            raise KeyBindingError(f"unknown action {action!r} in config.tui.keymap (known: {', '.join(sorted(_ACTIONS))})")
        bindings[action] = parse_key_binding(spec)
    return bindings


# ---------------------------------------------------------------------------
# Stream pump items
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StreamCompleted:
    """The runtime iterator returned normally; ``final_status`` is its last status."""

    final_status: str


@dataclass(frozen=True, slots=True)
class _StreamFailed:
    """The runtime iterator raised (transport failure)."""

    error: object


@dataclass(frozen=True, slots=True)
class _PolledChunks:
    """Background-notice replay fetched for ``session_id`` on the poll thread."""

    session_id: str
    chunks: tuple[RuntimeStreamChunk, ...]


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class TuiApp:
    """One root session, one terminal, one loop."""

    def __init__(
        self,
        workspace: Path,
        approval_mode: PermissionDecision | None = None,
        *,
        runtime: VoidCodeRuntime | None = None,
        keymap: Mapping[str, str] | None = None,
    ) -> None:
        self._workspace = workspace
        self._config: RuntimeConfig = load_runtime_config(workspace, approval_mode=approval_mode)
        self._runtime = runtime if runtime is not None else VoidCodeRuntime(workspace=workspace, config=self._config)
        # Fail loudly here, before the terminal is touched.
        self._bindings = parse_keymap(keymap if keymap is not None else self._configured_keymap())
        self._hints = KeyHints(expand=self._bindings["tools_expand"].name)

        self._term: Terminal | None = None
        self._theme: Theme | None = None
        self._view: SessionView | None = None
        self._status: StatusLine | None = None
        self._composer: Composer | None = None
        self._region: LiveRegion | None = None
        self._decoder = KeyDecoder()

        self._queue: queue.Queue[object] = queue.Queue()
        self._stream_thread: threading.Thread | None = None
        self._streaming = False
        self._polling = False
        self._session_id: str | None = None
        self._tracked_tasks: set[str] = set()
        self._next_poll_at = 0.0

        self._overlay: Overlay | None = None
        self._overlay_request_id = ""

        self._model = self._config.model or ""
        self._approval_mode = self._config.approval_mode or ""
        self._lsp_label = ""
        self._cost_usd = 0.0
        self._context_tokens = 0
        #: Provider context window (0 = unknown) and the session's reasoning
        #: effort, both refreshed outside the frame path.
        self._context_window = 0
        self._session_effort = ""

        self._quit = False
        self._input_closed = False
        self._cancel_requested = False
        self._escape_deadline = 0.0
        self._dirty = True
        self._next_frame = 0.0
        self._last_spinner = 0.0

    # -- construction ------------------------------------------------------

    def _configured_keymap(self) -> Mapping[str, str] | None:
        tui_config = self._config.tui
        return tui_config.keymap if tui_config is not None else None

    def _resolve_theme(self, terminal: Terminal) -> Theme:
        preferences = self._config.tui.preferences if self._config.tui is not None else None
        theme_preference = preferences.theme if preferences is not None else None
        # The runtime carries the palette name verbatim (no registry, no default);
        # ``resolve_theme`` owns validation and the mode fallback.
        return resolve_theme(
            theme_preference.name if theme_preference is not None else None,
            theme_preference.mode if theme_preference is not None and theme_preference.mode else "auto",
            color_system=terminal.color_system,
            glyph_preset="unicode" if terminal.unicode_ok else "ascii",
        )

    def _setup(self, terminal: Terminal) -> None:
        theme = self._resolve_theme(terminal)
        width = terminal.width
        self._theme = theme
        self._view = SessionView(theme=theme, hints=self._hints, width=width)
        self._status = StatusLine(theme, width)
        self._composer = Composer(
            theme=theme,
            width=width,
            placeholder="Ask voidcode...",
            completer=self._complete_slash_command,
        )
        self._region = LiveRegion(terminal.commit_rows, max_rows=terminal.height, paint=terminal.paint_frame)
        self._lsp_label = self._query_lsp_label()
        self._refresh_context_window()

    def _refresh_context_window(self) -> None:
        """Re-read the provider context window (setup, and after each turn).

        The gauge needs a window to divide the used tokens by; the same value the
        CLI surfaces (``provider_readiness`` -> ``context_window``). Never called
        from the frame path: it is one guard-wrapped runtime query per turn.
        """
        try:
            readiness = self._runtime.provider_readiness(session_id=self._session_id)
        except Exception as error:
            logger.error("Failed to query provider readiness: %s", error)
            return
        window = readiness.context_window
        if isinstance(window, int) and not isinstance(window, bool) and window > 0:
            self._context_window = window

    def _query_lsp_label(self) -> str:
        """The status line's LSP datum (the old sidebar's fifth panel)."""
        try:
            state = self._runtime.current_lsp_state()
        except Exception as error:
            logger.error("Failed to query LSP state: %s", error)
            return "lsp err"
        if state.mode != "managed":
            return "lsp off"
        active = [name for name, server in state.servers.items() if server.status == "running"]
        return f"lsp {len(active)}" if active else "lsp idle"

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        terminal = Terminal.open()
        self._term = terminal
        try:
            self._setup(terminal)
            self._loop(terminal)
        finally:
            self._shutdown(terminal)
        return 0

    # -- loop --------------------------------------------------------------

    def _loop(self, terminal: Terminal) -> None:
        while not self._quit:
            self._read_input(terminal)
            # A closed input side ends the session once the in-flight turn and
            # the pending frame are done; until then it keeps being serviced.
            if self._input_closed and not self._streaming and not self._dirty and self._queue.empty():
                return
            self._drain_queue()
            self._maybe_poll(time.monotonic())
            self._maybe_render()

    def _read_input(self, terminal: Terminal) -> None:
        """Fold whatever input is available; mark the input closed at its end.

        ``Terminal.has_input`` covers the "no descriptor at all" case, where a
        blocking-read loop would spin forever instead of ending.
        """
        if not terminal.has_input:
            self._input_closed = True
            return
        now = time.monotonic()
        data = terminal.read_bytes(self._read_timeout(now))
        if data:
            self._escape_deadline = now + _ESCAPE_FLUSH_SECONDS
            for key in self._decoder.feed(data):
                self._handle_key(key)
                if self._quit:
                    return
            return
        if terminal.eof:
            self._input_closed = True
            for key in self._decoder.flush():
                self._handle_key(key)
            return
        if self._escape_deadline and now >= self._escape_deadline:
            self._escape_deadline = 0.0
            for key in self._decoder.flush():
                self._handle_key(key)

    def _read_timeout(self, now: float) -> float:
        """Short enough to service a pending frame, long enough to stay cheap."""
        if self._queue.qsize() > _WRITE_BACKLOG_LIMIT:
            # Backlogged: do not wait for input at all, drain first.
            return 0.0
        if self._streaming or self._dirty:
            return _ACTIVE_POLL_SECONDS
        timeout = _IDLE_POLL_SECONDS
        if self._tracked_tasks and self._session_id is not None:
            # Floor at the active timeout: a poll that is due (or overdue) must be
            # serviced promptly, without a zero-timeout spin.
            timeout = min(timeout, max(_ACTIVE_POLL_SECONDS, self._next_poll_at - now))
        return timeout

    def _maybe_render(self) -> None:
        assert self._term is not None
        now = time.monotonic()
        if self._term.resize_pending():
            self._apply_resize()
        self._tick_spinner(now)
        if not self._dirty or now < self._next_frame:
            return
        if self._queue.qsize() > _WRITE_BACKLOG_LIMIT:
            # A backlog means more events are already waiting: compose one fresh
            # frame after draining instead of painting every intermediate one.
            self._next_frame = now + _BACKLOG_RETRY_SECONDS
            return
        started = time.monotonic()
        self._render()
        ended = time.monotonic()
        self._dirty = False
        self._schedule_next_frame(started, ended)

    def _schedule_next_frame(self, started: float, ended: float) -> None:
        cost = ended - started
        interval = _MIN_FRAME_SECONDS if cost <= _MIN_FRAME_SECONDS else min(_MAX_FRAME_SECONDS, cost * 2)
        self._next_frame = ended + interval

    def _tick_spinner(self, now: float) -> None:
        """Advance live tool spinners while a turn streams (the liveness signal)."""
        if not self._streaming or now - self._last_spinner < _SPINNER_ADVANCE_SECONDS:
            return
        self._last_spinner = now
        for block in self._view_blocks():
            if isinstance(block, ToolBlock) and block.streaming:
                block.spinner_frame += 1
                self._dirty = True

    def _apply_resize(self) -> None:
        assert self._term is not None and self._view is not None
        width = self._term.width
        self._view.set_width(width)
        if self._status is not None:
            self._status.set_width(width)
        if self._composer is not None:
            self._composer.set_width(width)
        if self._region is not None:
            self._region.clear()
        self._dirty = True

    # -- frames ------------------------------------------------------------

    def _render(self) -> None:
        assert self._term is not None and self._region is not None and self._view is not None
        if self._term.alt_screen:
            # Alt-screen borrow: paint the overlay only, never the scrollback.
            self._term.paint_frame(self._overlay_rows())
            return
        settled = self._view.transcript().take_settled()
        if not self._term.is_tty:
            # Off-tty the terminal is a log, not a screen: only durable rows are
            # written (no live region, no status line, no composer, no escapes).
            if settled:
                self._region.commit(settled)
            return
        if settled:
            self._region.commit(settled)
            # The terminal erased the live region with the commit; drop the stale
            # ledger too so the next frame is painted in full.
            self._region.clear()
        self._region.set_live(self._live_frame())

    def _live_frame(self) -> Sequence[str]:
        assert self._view is not None and self._status is not None and self._composer is not None
        rows = list(self._view.transcript().live_rows())
        if rows:
            rows.append("")
        status = self._status.render(self._status_data(self._view.view_state()))
        if status:
            rows.append(status)
        rows.extend(self._overlay_rows() if self._overlay is not None else self._composer.render())
        return rows

    def _overlay_rows(self) -> list[str]:
        assert self._term is not None
        if self._overlay is None:
            return []
        rows = list(self._overlay.render(self._term.width))
        if self._term.alt_screen and len(rows) > self._term.height:
            # ponytail: alt-screen overlays are top-cropped (no viewport); the
            # session picker's type-to-filter is the long-list path.
            rows = rows[: self._term.height]
        return rows

    def _status_data(self, state: ViewState) -> StatusSegmentData:
        window = self._context_window
        # Thinking level: session metadata first, effective config as fallback.
        return replace(
            state.status,
            model=self._model,
            thinking=self._session_effort or self._config.reasoning_effort or "off",
            mode=self._approval_mode,
            path=str(self._workspace),
            lsp=self._lsp_label,
            cost_usd=self._cost_usd,
            context_percent=self._context_tokens / window * 100 if window else None,
            context_window=window,
            context_tokens=self._context_tokens,
        )

    # -- events ------------------------------------------------------------

    def _drain_queue(self) -> None:
        for _ in range(_DRAIN_BATCH):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, RuntimeStreamChunk):
                self._consume_chunk(item)
            elif isinstance(item, _PolledChunks):
                self._consume_polled(item)
            elif isinstance(item, _StreamCompleted):
                self._finish_stream(item.final_status)
            elif isinstance(item, _StreamFailed):
                self._fail_stream(item.error)

    def _consume_chunk(self, chunk: RuntimeStreamChunk) -> None:
        assert self._view is not None
        self._session_id = chunk.session.session.id
        if self._cancel_requested:
            # ``escape`` arrived before the runtime allocated the session id.
            self._cancel_requested = False
            self._cancel_active_run()
        self._harvest_metadata(chunk.session.metadata)
        if chunk.kind == "event" and chunk.event is not None:
            event = chunk.event
            self._track_background_task(event)
            if self._view.apply_event(event):
                self._dirty = True
            self._take_pending_overlay()
        elif chunk.kind == "output" and chunk.output is not None:
            self._consume_output(chunk.output)

    def _consume_output(self, output: str) -> None:
        """The graph writes the complete answer after the provider deltas.

        Render it only when no delta was streamed for this attempt, or the answer
        would appear twice (the old app's ``_streamed_provider_text`` rule; the
        view owns the flag so a retraction resets it in one place).
        """
        assert self._view is not None
        if self._view.streamed_provider_text:
            return
        self._view.transcript().add(AssistantBlock(text=output, settled=False))
        self._dirty = True

    def _consume_polled(self, item: _PolledChunks) -> None:
        if item.session_id != self._session_id:
            return
        for chunk in item.chunks:
            self._consume_chunk(chunk)

    def _finish_stream(self, final_status: str) -> None:
        assert self._view is not None
        self._streaming = False
        self._cancel_requested = False
        self._view.finish_stream(final_status)
        self._refresh_context_window()
        self._dirty = True

    def _fail_stream(self, error: object) -> None:
        assert self._view is not None
        self._streaming = False
        self._cancel_requested = False
        self._view.finish_stream("failed", error=error)
        self._refresh_context_window()
        self._dirty = True

    def _harvest_metadata(self, metadata: Mapping[str, object] | None) -> None:
        """Status-line data the runtime already delivers per chunk."""
        if not metadata:
            return
        model, _provider = session_model_identity(metadata)
        if model:
            self._model = model
        effort = metadata.get("reasoning_effort")
        if isinstance(effort, str) and effort:
            self._session_effort = effort
        conversation = metadata.get("context_window")
        if isinstance(conversation, Mapping):
            tokens = conversation.get("usage_tokens_after", conversation.get("usage_tokens_before"))
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                self._context_tokens = tokens
        usage = metadata.get("provider_usage")
        cumulative = usage.get("cumulative") if isinstance(usage, Mapping) else None
        cost = cumulative.get("cost_usd") if isinstance(cumulative, Mapping) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self._cost_usd = float(cost)

    def _track_background_task(self, event: EventEnvelope) -> None:
        payload = event.payload or {}
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return
        if event.event_type == "runtime.tool_completed" and payload.get("tool") == "task":
            if payload.get("status") in {"ok", "queued", "running"}:
                self._tracked_tasks.add(task_id)
        elif event.event_type in _BACKGROUND_TERMINAL_EVENTS:
            self._tracked_tasks.discard(task_id)

    # -- runtime threads ---------------------------------------------------

    def _start_stream(self, open_stream: Callable[[], Iterator[RuntimeStreamChunk]]) -> None:
        self._streaming = True
        self._stream_thread = threading.Thread(target=self._pump_stream, args=(open_stream,), name="voidcode-tui-stream", daemon=True)
        self._stream_thread.start()

    def _pump_stream(self, open_stream: Callable[[], Iterator[RuntimeStreamChunk]]) -> None:
        """Drain one blocking runtime iterator into the queue (worker thread)."""
        last_status = "Idle"
        saw_chunk = False
        try:
            for chunk in open_stream():
                saw_chunk = True
                last_status = chunk.session.status
                self._queue.put(chunk)
            if not saw_chunk:
                raise ValueError("runtime stream emitted no chunks")
        except Exception as error:
            self._queue.put(_StreamFailed(error))
            return
        self._queue.put(_StreamCompleted(last_status))

    def _maybe_poll(self, now: float) -> None:
        """Replay the parent session's tail while idle with tracked background work.

        Background/delegated notices are persisted events on the *parent* session
        and the runtime has no subscription, so an idle client has to ask -- this
        is the old app's 1 s poll, ported. The view's sequence dedupe drops the
        events already applied.
        """
        if self._polling or self._streaming or self._session_id is None or not self._tracked_tasks:
            return
        if now < self._next_poll_at:
            return
        self._polling = True
        self._next_poll_at = now + _BACKGROUND_POLL_SECONDS
        session_id = self._session_id
        threading.Thread(target=self._poll_worker, args=(session_id,), name="voidcode-tui-poll", daemon=True).start()

    def _poll_worker(self, session_id: str) -> None:
        try:
            chunks = tuple(self._runtime.resume_stream(session_id=session_id))
        except Exception as error:
            logger.warning("Failed to poll parent session events: %s", error)
        else:
            self._queue.put(_PolledChunks(session_id, chunks))
        finally:
            self._polling = False

    # -- keys --------------------------------------------------------------

    def _handle_key(self, key: Key) -> None:
        if self._overlay is not None:
            self._handle_overlay_key(key)
            return
        for action, binding in self._bindings.items():
            if key.name == binding.name:
                self._run_action(action)
                return
        assert self._composer is not None
        outcome = self._composer.handle_key(key)
        if outcome.action is ComposerAction.SUBMIT:
            self._submit(outcome.text)
        elif outcome.action is ComposerAction.CANCEL_TURN:
            self._cancel_turn()
        elif outcome.action is ComposerAction.INTERRUPT:
            self._quit = True
        # Any key may have moved the cursor or changed the draft/completion; the
        # frame diff makes an unchanged repaint free.
        self._dirty = True

    def _run_action(self, action: str) -> None:
        if action == "tools_expand":
            self._toggle_expand()
        elif action == "session_new":
            self._command_session_new()
        else:
            self._command_session_resume()

    def _handle_overlay_key(self, key: Key) -> None:
        overlay = self._overlay
        assert overlay is not None
        outcome = overlay.handle_key(key)
        if outcome.kind is OverlayOutcomeKind.PENDING:
            self._dirty = True
            return
        if isinstance(overlay, ApprovalOverlay):
            # Every dismissal path (``escape``, a cancel outcome) denies: the old
            # modal bound escape to deny and treated a ``None`` dismissal as deny.
            decision = "allow" if outcome.kind is OverlayOutcomeKind.DONE and outcome.payload == "allow" else "deny"
            request_id = self._overlay_request_id
            self._close_overlay()
            self._resolve_approval(request_id, decision)
        elif isinstance(overlay, QuestionOverlay):
            payload = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            request_id = self._overlay_request_id
            self._close_overlay()
            if isinstance(payload, tuple):
                self._answer_question(request_id, payload)
        else:
            session_id = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(session_id, str) and session_id:
                self._open_resumed_session(session_id)

    # -- flows -------------------------------------------------------------

    def _submit(self, text: str) -> None:
        prompt = text.strip()
        if not prompt:
            return
        if prompt.startswith("/"):
            self._slash_command(prompt)
            return
        if self._streaming or self._overlay is not None:
            self._steer(prompt)
            return
        self._start_prompt(prompt)

    def _start_prompt(self, prompt: str) -> None:
        assert self._view is not None
        self._view.transcript().add(UserBlock(text=prompt))
        request = RuntimeRequest(
            prompt=prompt,
            session_id=self._session_id,
            allocate_session_id=self._session_id is None,
            metadata={"provider_stream": True},
        )
        self._start_stream(lambda: self._runtime.run_stream(request))
        self._dirty = True

    def _steer(self, prompt: str) -> None:
        if self._session_id is None:
            self._notice("✖ Cannot queue steering without an active session")
            return
        try:
            queued = self._runtime.queue_steering(self._session_id, prompt)
        except Exception as error:
            logger.exception("Failed to persist TUI steering message")
            self._notice(f"✖ Runtime rejected steering message: {format_runtime_error(error)}")
            return
        self._notice(f"↳ Steering queued ({len(queued)}): {prompt}")

    def _resolve_approval(self, request_id: str, decision: str) -> None:
        assert self._view is not None
        self._view.resolve_overlay(request_id)
        if self._session_id is None:
            self._notice("✖ Approval cannot be answered without a session")
            return
        session_id = self._session_id
        self._start_stream(
            lambda: self._runtime.resume_stream(
                session_id=session_id,
                approval_request_id=request_id,
                approval_decision="allow" if decision == "allow" else "deny",
            )
        )
        self._dirty = True

    def _answer_question(self, request_id: str, payload: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
        assert self._view is not None
        self._view.resolve_overlay(request_id)
        responses = tuple(QuestionResponse(header=header, answers=tuple(answers)) for header, answers in payload)
        if self._session_id is None:
            self._notice("✖ Question cannot be answered without a session")
            return
        session_id = self._session_id
        self._start_stream(
            lambda: self._runtime.answer_question_stream(
                session_id=session_id,
                question_request_id=request_id,
                responses=responses,
            )
        )
        self._dirty = True

    def _command_session_new(self) -> None:
        assert self._view is not None
        self._view.reset_for_new_session()
        self._view.transcript().add(SessionMarkerBlock(label="New Session"))
        self._session_id = None
        self._model = self._config.model or ""
        self._session_effort = ""
        self._cost_usd = 0.0
        self._context_tokens = 0
        self._tracked_tasks.clear()
        self._next_poll_at = 0.0
        self._dirty = True

    def _command_session_resume(self) -> None:
        try:
            sessions = self._runtime.list_sessions()
        except Exception as error:
            logger.error("Failed to list sessions: %s", error)
            self._notice(f"✖ Failed to list sessions: {format_runtime_error(error)}")
            return
        entries = [(summary.session.id, summary.prompt) for summary in sessions]
        self._overlay = SessionPickerOverlay(sessions=entries, theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _open_resumed_session(self, session_id: str) -> None:
        assert self._view is not None
        self._view.reset_for_new_session()
        self._session_id = session_id
        self._session_effort = ""
        self._tracked_tasks.clear()
        self._next_poll_at = 0.0
        short_id = short_session_id(session_id)
        self._view.transcript().add(SessionMarkerBlock(label=f"Resumed Session {short_id}"))
        self._start_stream(lambda: self._runtime.resume_stream(session_id=session_id))
        self._dirty = True

    def _cancel_turn(self) -> None:
        """``escape``: interrupt the active run through the runtime's cancel surface."""
        if not self._streaming:
            return
        if self._session_id is None:
            # The runtime allocates the session id and only the first chunk carries
            # it: remember the press instead of dropping it.
            self._cancel_requested = True
            self._notice("■ Turn cancel requested")
            return
        self._cancel_active_run()

    def _cancel_active_run(self) -> None:
        assert self._session_id is not None
        try:
            result = self._runtime.cancel_session(self._session_id, reason="tui_turn_cancel")
        except Exception as error:
            logger.error("Failed to cancel the active run: %s", error)
            self._notice(f"✖ Cancel failed: {format_runtime_error(error)}")
            return
        self._notice("■ Turn cancel requested" if result.interrupted else "■ No active run to cancel")

    # -- slash commands ----------------------------------------------------

    def _complete_slash_command(self, word: str) -> Sequence[str]:
        if not word.startswith("/"):
            return ()
        lowered = word.lower()
        return [command for command in _SLASH_COMMANDS if command.startswith(lowered)]

    def _slash_command(self, raw: str) -> None:
        command, _, argument = raw.partition(" ")
        if command.lower() == "/expand":
            self._expand(argument.strip())
            return
        self._notice(f"✖ Unknown command: {command}")

    def _expand(self, tool_call_id: str) -> None:
        """``/expand <tool_call_id>``: artifact first, cached content second."""
        assert self._view is not None
        if not tool_call_id:
            self._view.expand_tool("", "")
            self._dirty = True
            return
        content: str | None = None
        artifact_failed = False
        artifact_id = self._view.pending_tool_artifact(tool_call_id)
        if artifact_id is not None and self._session_id is not None:
            result: Mapping[str, object] | None
            try:
                result = self._runtime.read_tool_output_artifact(
                    session_id=self._session_id,
                    tool_call_id=tool_call_id,
                    limit=_ARTIFACT_READ_LIMIT,
                )
            except Exception as error:
                logger.error("Failed to read tool output artifact: %s", error)
                artifact_failed = True
                result = None
            if isinstance(result, Mapping) and result.get("status") == "available":
                candidate = result.get("content")
                if isinstance(candidate, str):
                    content = candidate
        if content is None:
            content = self._view.tool_content(tool_call_id)
            if content is not None and artifact_failed:
                self._notice("⚠ Artifact read failed; showing cached output")
        if content is None:
            self._notice(f"✖ No stored output for tool_call_id: {tool_call_id}")
            return
        self._view.expand_tool(tool_call_id, content)
        self._rewrite_settled()

    def _toggle_expand(self) -> None:
        """``tools_expand``: expand or collapse every block.

        Expanding a block that was already committed rewrites rows that live in
        native scrollback, which cannot be rewritten: the settled tape is printed
        once more, expanded (see :meth:`_rewrite_settled`).
        """
        assert self._view is not None
        blocks = self._view.transcript().blocks
        if not blocks:
            return
        self._view.transcript().set_expanded(not all(block.expanded for block in blocks))
        self._rewrite_settled()

    def _rewrite_settled(self) -> None:
        """Re-print the settled tape after a change inside the committed prefix.

        Inline scrollback cannot be rewritten, and ``take_settled``'s row cursor
        cannot see an edit that kept the total length: without the rewind it would
        hand out a misaligned suffix and commit garbage rows.
        """
        assert self._view is not None
        self._view.transcript().rewind()
        self._dirty = True

    def _notice(self, text: str) -> None:
        assert self._view is not None
        self._view.transcript().add(NoticeBlock(text=text))
        self._dirty = True

    # -- overlays ----------------------------------------------------------

    def _take_pending_overlay(self) -> None:
        assert self._view is not None
        if self._overlay is not None:
            return
        pending = self._view.take_pending_overlay()
        if pending is None:
            return
        if isinstance(pending, ApprovalRequest):
            self._overlay = ApprovalOverlay(
                tool=pending.tool,
                target=pending.target,
                reason=pending.reason,
                arguments=pending.arguments,
                theme=self._theme,
            )
        elif isinstance(pending, QuestionRequest):
            self._overlay = QuestionOverlay(questions=pending.questions, theme=self._theme)
        else:
            return
        self._overlay_request_id = pending.request_id
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _enter_alt_screen(self) -> None:
        assert self._term is not None and self._region is not None
        if self._term.alt_screen or self._overlay is None:
            return
        if not self._overlay.wants_fullscreen(self._term.width, self._term.height):
            return
        # The live rows must not leak into the alternate buffer.
        self._region.clear()
        self._term.enter_alt_screen()

    def _close_overlay(self) -> None:
        self._overlay = None
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(True)
        if self._term is not None and self._term.alt_screen:
            self._term.leave_alt_screen()
            if self._region is not None:
                self._region.clear()
        # An approval/question that arrived while this overlay owned the keyboard
        # is still pending: open it now instead of leaving the turn stuck.
        self._take_pending_overlay()
        self._dirty = True

    # -- helpers -----------------------------------------------------------

    def _view_blocks(self) -> Sequence[object]:
        if self._view is None:
            return ()
        return self._view.transcript().blocks

    def _shutdown(self, terminal: Terminal) -> None:
        if self._streaming and self._session_id is not None:
            try:
                self._runtime.cancel_session(self._session_id, reason="tui_shutdown")
            except Exception as error:
                logger.warning("Failed to cancel the active run on shutdown: %s", error)
        thread = self._stream_thread
        if thread is not None and thread.is_alive():
            thread.join(_SHUTDOWN_JOIN_SECONDS)
        try:
            self._runtime.__exit__(None, None, None)
        except Exception as error:
            logger.error("Failed to shut down runtime: %s", error)
        finally:
            terminal.close()


def run_tui(
    *,
    workspace: Path,
    approval_mode: PermissionDecision | None = None,
    runtime: VoidCodeRuntime | None = None,
    keymap: Mapping[str, str] | None = None,
) -> int:
    """Run the inline TUI until the user exits; returns the process exit code."""
    try:
        app = TuiApp(
            workspace,
            approval_mode,
            runtime=runtime,
            keymap=keymap,
        )
    except KeyBindingError as error:
        print(f"voidcode tui: {error}", file=sys.stderr)
        return 2
    return app.run()
