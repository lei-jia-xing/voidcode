"""Inline TUI: the app loop that owns the terminal and drives the runtime client.

The renderer is split into pure layers (``term`` / ``region`` / ``transcript`` /
``statusline`` / ``composer`` / ``overlay`` / ``events``); this module is the only
place that knows all of them, and the only place that decides *when* a frame is
written.

Contracts implemented here:

* **Commit once.** Rows of settled blocks leave the app exactly once:
  ``Transcript.take_settled()`` -> ``LiveRegion.commit()`` ->
  ``Terminal.commit_rows()`` (native scrollback, never repainted). The tape owns
  that ledger itself (it marks the last block handed out), so no caller -- not
  ``ctrl+o``, not a resize -- can re-emit a committed prefix or misalign a later
  batch. A toggle that lands behind the commit boundary cannot be redrawn, and
  ``Transcript.set_expanded`` reports that so the app can say so. ``LiveRegion``
  only trims unsettled transcript rows for the viewport; it never commits them.
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

from pydantic import ValidationError

from ..command.native import INIT_PROMPT, NATIVE_COMMANDS_BY_NAME
from ..core.questions import QuestionCancellation, QuestionResponse
from ..core.tool_context import (
    ArtifactBinary,
    ArtifactInvalid,
    ArtifactMissing,
    ArtifactPage,
    ArtifactUnavailable,
    NextPage,
    TranscriptPage,
)
from ..runtime.config.loading import (
    RuntimeConfig,
    RuntimeTuiResolvedConfig,
    RuntimeTuiThemeConfig,
    load_runtime_config,
    save_tui_appearance_config,
)
from ..runtime.config.models import (
    VALID_TUI_COMMANDS,
    VALID_TUI_COMPOSER_CURSOR_SHAPES,
    VALID_TUI_COMPOSER_STYLES,
    VALID_TUI_THEME_MODES,
)
from ..runtime.contracts import (
    CopyTarget,
    ReviewCommitLogEntry,
    ReviewTarget,
    RuntimeRequest,
    RuntimeStreamChunk,
    SessionContextUsage,
    SessionExport,
    SessionStatusPanel,
)
from ..runtime.events import EventEnvelope
from ..runtime.permissions.policy import ApprovalMode
from ..runtime.service import VoidCodeRuntime
from ..runtime.session_metadata_helpers import session_model_identity
from ..runtime.user_shell import USER_SHELL_HELP_HINT, USER_SHELL_HELP_TITLE, parse_shell_escape
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
    AppearanceOverlay,
    ApprovalOverlay,
    CopyPickerOverlay,
    ExportPathOverlay,
    ExportPickerOverlay,
    GoalConfirmOverlay,
    HelpOverlay,
    Overlay,
    OverlayOutcome,
    OverlayOutcomeKind,
    PromptHistoryOverlay,
    QuestionOverlay,
    RenameSessionOverlay,
    ReviewBranchPickerOverlay,
    ReviewCommitPickerOverlay,
    ReviewCustomPromptOverlay,
    ReviewPresetPickerOverlay,
    SessionPickerOverlay,
    SideQuestionOverlay,
    ToolArtifactOverlay,
    TreeSelectorOverlay,
)
from .region import LiveRegion, TuiFrame
from .statusline import StatusLine, StatusSegmentData, statusline_profile_segments, statusline_profiles
from .term import Terminal, TerminalCapabilityError, copy_to_clipboard, escape_terminal_controls
from .theme import FG_TOKENS, Theme, background_mode, palette_names, resolve_theme, validate_theme_configuration
from .transcript import (
    AssistantBlock,
    DiffBlock,
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
_ACTIONS: Final[frozenset[str]] = frozenset(VALID_TUI_COMMANDS)

_DEFAULT_KEYMAP: Final[Mapping[str, str]] = MappingProxyType({"app.tools.expand": "ctrl+o"})

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


class TuiConfigurationError(ValueError):
    """A user configuration failure found before entering terminal mode."""


def parse_keymap(keymap: Mapping[str, str] | None) -> dict[str, Key]:
    """Resolve ``config.tui.keymap`` into action -> :class:`Key`, or fail loudly.

    The config maps a key chord to an action (``{"ctrl+o": "app.tools.expand"}``);
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
class _StreamClosed:
    """The runtime iterator returned normally; closure is not a run outcome."""

    last_status: str


@dataclass(frozen=True, slots=True)
class _StreamFailed:
    """The runtime iterator raised; its last observed status is not terminal proof."""

    error: object
    last_status: str


@dataclass(frozen=True, slots=True)
class _PolledChunks:
    """Background-notice replay fetched for ``session_id`` on the poll thread.

    A replay is history, not a live stream: the runtime answers a bare
    ``resume_stream`` with the persisted events *plus* one ``kind="output"``
    chunk carrying the session's stored answer. The events are deduped by
    sequence, but that output chunk is not an event, so it is handled by
    :meth:`TuiApp._consume_output`'s replay rule rather than the live one.
    """

    session_id: str
    chunks: tuple[RuntimeStreamChunk, ...]


@dataclass(frozen=True, slots=True)
class _NativeCompactResult:
    session_id: str
    result: dict[str, object] | None = None
    error: object | None = None


@dataclass(frozen=True, slots=True)
class _NativePlanResult:
    session_id: str
    result: dict[str, object] | None = None
    error: object | None = None


@dataclass(frozen=True, slots=True)
class _NativeReviewResult:
    session_id: str
    output: str | None = None
    error: object | None = None


@dataclass(frozen=True, slots=True)
class _NativeShellResult:
    session_id: str
    result: dict[str, object] | None = None
    error: object | None = None


@dataclass(frozen=True, slots=True)
class _NativeBtwResult:
    session_id: str
    question: str = ""
    answer: str | None = None
    error: object | None = None


@dataclass(frozen=True, slots=True)
class _NativeInfoResult:
    session_id: str
    kind: str = ""
    result: SessionStatusPanel | SessionExport | tuple[CopyTarget, ...] | SessionContextUsage | None = None
    error: object | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class TuiApp:
    """One root session, one terminal, one loop."""

    def __init__(
        self,
        workspace: Path,
        approval_mode: ApprovalMode | None = None,
        *,
        runtime: VoidCodeRuntime | None = None,
        keymap: Mapping[str, str] | None = None,
    ) -> None:
        self._workspace = workspace
        try:
            self._config: RuntimeConfig = load_runtime_config(workspace, approval_mode=approval_mode)
            self._validate_tui_appearance(self._config.tui)
        except (ValidationError, ValueError) as error:
            raise TuiConfigurationError(f"invalid runtime configuration: {error}") from error
        self._tui_config = self._config.tui or RuntimeTuiResolvedConfig()
        # Fail loudly here, before the terminal is touched.
        self._bindings = parse_keymap(keymap if keymap is not None else self._configured_keymap())
        self._hints = KeyHints(expand=self._bindings["app.tools.expand"].name)
        self._runtime = runtime if runtime is not None else VoidCodeRuntime(workspace=workspace, config=self._config)

        self._resolved_theme_mode = "dark"
        self._auto_theme_fallback = False
        self._auto_theme_source = "not queried"
        self._auto_resolved_mode: str | None = None
        self._appearance_original: RuntimeTuiResolvedConfig | None = None
        self._appearance_saved = True
        self._appearance_original_saved: bool | None = None
        self._term: Terminal | None = None
        self._layout_size: tuple[int, int] | None = None
        self._theme: Theme | None = None
        self._view: SessionView | None = None
        self._status: StatusLine | None = None
        self._composer: Composer | None = None
        self._region: LiveRegion | None = None
        self._decoder = KeyDecoder()

        self._queue: queue.Queue[object] = queue.Queue()
        self._stream_thread: threading.Thread | None = None
        self._streaming = False
        self._compact_pending = False
        self._plan_pending = False
        self._review_pending = False
        self._compact_thread: threading.Thread | None = None
        self._plan_thread: threading.Thread | None = None
        self._review_thread: threading.Thread | None = None
        self._btw_pending = False
        self._btw_recent: dict[str, tuple[str, str]] = {}
        self._info_pending: set[str] = set()
        self._copy_targets: list[tuple[str, str, str]] = []
        self._export_markdown: str = ""
        self._goal_status: str | None = None
        self._polling = False
        self._session_id: str | None = None
        self._session_name: str | None = None
        self._tracked_tasks: set[str] = set()
        self._next_poll_at = 0.0

        self._overlay: Overlay | None = None
        self._overlay_request_id = ""
        self._tool_picker_active = False
        self._artifact_detail: tuple[str, str | None, str | None, str] | None = None
        self._artifact_offsets: list[int] = []
        self._artifact_next_offset: int | None = None

        self._model = self._config.model or ""
        self._plan_mode = False
        self._approval_mode = self._config.approval_mode or ""
        self._lsp_label = ""
        self._cost_usd: float | None = None
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._cache_read_tokens: int | None = None
        self._cache_write_tokens: int | None = None
        self._cache_hit_rate: float | None = None
        self._context_tokens: int | None = None
        #: Provider context window (0 = unknown) and the session's reasoning
        #: effort, both refreshed outside the frame path.
        self._context_window = 0
        self._session_effort = ""

        self._quit = False
        self._input_closed = False
        self._cancel_requested = False
        #: Transient hint for a ``ctrl+o`` toggle that only reached rows already
        #: in scrollback. It rides the live region (repainted, never committed),
        #: like omp's ``showStatus``, so it cannot grow the scrollback.
        self._expand_notice = ""
        self._escape_deadline = 0.0
        self._dirty = True
        self._next_frame = 0.0
        self._last_spinner = 0.0

    # -- construction ------------------------------------------------------

    def _configured_keymap(self) -> Mapping[str, str] | None:
        return self._tui_config.keymap

    def _validate_tui_appearance(self, config: RuntimeTuiResolvedConfig | None) -> None:
        if config is None:
            return
        validate_theme_configuration(config.theme)
        for segment, option in config.statusline.options.items():
            if option.color is not None and option.color not in FG_TOKENS:
                raise ValueError(f"tui.statusline.options.{segment}.color must name a foreground theme token")

    def _resolve_theme(self, terminal: Terminal, settings: RuntimeTuiThemeConfig | None = None) -> Theme:
        settings = settings or self._tui_config.theme
        mode = settings.mode
        if mode == "auto":
            if self._auto_resolved_mode is None:
                background = terminal.query_default_background_rgb()
                detected = background_mode(background)
                self._auto_theme_fallback = detected is None
                self._auto_theme_source = terminal.background_source
                self._auto_resolved_mode = detected or "dark"
            mode = self._auto_resolved_mode
        self._resolved_theme_mode = mode
        name = settings.dark if mode == "dark" else settings.light
        glyph_preset = settings.symbols_preset
        if not terminal.unicode_ok and glyph_preset != "ascii":
            glyph_preset = "ascii"
        return resolve_theme(
            name,
            mode,
            color_system=terminal.color_system,
            glyph_preset=glyph_preset,
            custom=settings.custom,
            symbol_overrides=settings.symbols_overrides,
        )

    def _setup(self, terminal: Terminal) -> None:
        theme = self._resolve_theme(terminal)
        width, height = terminal.width, terminal.height
        self._layout_size = (width, height)
        self._theme = theme
        self._view = SessionView(theme=theme, hints=self._hints, width=width)
        tui_config = self._tui_config
        status_config = tui_config.statusline
        self._composer = Composer(
            theme=theme,
            width=width,
            style=tui_config.composer.style,
            gutter=tui_config.composer.gutter,
            padding=tui_config.composer.padding,
        )
        self._composer.set_max_height(self._composer.input_height(height))
        self._status = StatusLine(theme, self._composer.status_width(), status_config)
        terminal.set_cursor_shape(tui_config.composer.cursor_shape, tui_config.composer.cursor_blink)
        self._region = LiveRegion(terminal.commit_rows, max_rows=height, paint=terminal.paint_frame)
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
            return ""
        active = [name for name, server in state.servers.items() if server.status == "running"]
        return f"lsp {len(active)}" if active else ""

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        self._runtime.__enter__()
        terminal: Terminal | None = None
        try:
            terminal = Terminal.open()
            self._term = terminal
            self._setup(terminal)
            self._loop(terminal)
        finally:
            if terminal is None:
                try:
                    self._runtime.__exit__(None, None, None)
                except Exception as error:
                    logger.error("Failed to shut down runtime: %s", error)
            else:
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
        if self._term.resize_pending() or (self._term.width, self._term.height) != self._layout_size:
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
        width, height = self._term.width, self._term.height
        self._layout_size = (width, height)
        self._view.set_width(width)
        if self._composer is not None:
            self._composer.set_width(width)
            self._composer.set_max_height(self._composer.input_height(height))
        if self._status is not None:
            self._status.set_width(self._composer.status_width() if self._composer is not None else width)
        if self._region is not None:
            self._region.set_max_rows(height)
            self._region.clear()
        self._dirty = True

    def _below_minimum_size(self) -> bool:
        return self._term is not None and (self._term.width < 20 or self._term.height < 6)

    def _safe_hold_rows(self) -> TuiFrame:
        text = "Resize to >=20x6"
        row = self._theme.fg("warning", text) if self._theme is not None else text
        return TuiFrame(chrome_rows=(row,))

    # -- frames ------------------------------------------------------------

    def _render(self) -> None:
        assert self._term is not None and self._region is not None and self._view is not None
        if self._term.is_tty and self._below_minimum_size():
            self._term.paint_frame(self._safe_hold_rows())
            return
        if self._term.alt_screen:
            self._term.paint_frame(self._overlay_frame())
            return
        settled = self._view.transcript().take_settled()
        if not self._term.is_tty:
            if settled:
                self._region.commit(settled)
            return
        if settled:
            self._region.commit(settled)
            self._region.clear()
        frame = self._live_frame()
        self._region.set_live(frame)

    def _live_frame(self) -> TuiFrame:
        assert self._view is not None and self._status is not None and self._composer is not None and self._theme is not None
        assert self._term is not None
        transcript = list(self._view.transcript().live_rows())
        if transcript:
            transcript.append("")
        chrome: list[str] = []
        cursor: tuple[int, int] | None = None
        status = self._status.render(self._status_data(self._view.view_state()))
        if self._overlay is None:
            composer_rows = list(self._composer.render(status=status))
            extra_rows = max(0, self._term.height - len(composer_rows))
            brand = self._session_id is None and not self._view.transcript().blocks and self._tui_config.brand_enabled
            notice = self._expand_notice
            if notice and extra_rows:
                chrome.append(self._theme.fg("dim", notice))
                extra_rows -= 1
            if brand and extra_rows:
                mark = "[*V]" if not self._term.unicode_ok or self._theme.glyphs.preset == "ascii" else "◇V"
                chrome.insert(0, f"  {self._theme.fg('accent', mark)}  {self._theme.fg('accent', 'VOIDCODE')}")
            transcript_height = max(0, self._term.height - len(chrome) - len(composer_rows))
            transcript = transcript[-transcript_height:] if transcript_height else []
            if self._composer.cursor_position is not None:
                row, column = self._composer.cursor_position
                cursor = (len(transcript) + len(chrome) + row, column)
            chrome.extend(composer_rows)
        else:
            if status:
                chrome.append(status)
            if self._expand_notice:
                chrome.append(self._theme.fg("dim", self._expand_notice))
            frame = self._overlay_frame()
            if frame.cursor is not None:
                row, column = frame.cursor
                cursor = (len(transcript) + len(chrome) + row, column)
            chrome.extend(frame.chrome_rows)
        frame = TuiFrame(tuple(transcript), tuple(chrome), cursor)
        return frame

    def _overlay_frame(self) -> TuiFrame:
        assert self._term is not None
        if self._overlay is None:
            return TuiFrame()
        rows = self._overlay.viewport(self._term.width, self._term.height)
        cursor = self._overlay.cursor_position(self._term.width)
        return TuiFrame(chrome_rows=tuple(rows), cursor=cursor)

    def _status_data(self, state: ViewState) -> StatusSegmentData:
        window = self._context_window
        # Thinking level: session metadata first, effective config as fallback.
        return replace(
            state.status,
            session_name=self._session_name or short_session_id(self._session_id),
            model=self._model,
            thinking=self._session_effort or self._config.reasoning_effort or "off",
            mode=(f"{self._approval_mode} · plan" if self._plan_mode else self._approval_mode)
            + (f" · goal:{self._goal_status}" if self._goal_status else ""),
            path=str(self._workspace),
            lsp=self._lsp_label,
            cost_usd=self._cost_usd,
            context_percent=(self._context_tokens / window * 100 if window and self._context_tokens is not None else None),
            context_window=window,
            context_tokens=self._context_tokens,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cache_read_tokens=self._cache_read_tokens,
            cache_write_tokens=self._cache_write_tokens,
            cache_hit_rate=self._cache_hit_rate,
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
            elif isinstance(item, _StreamClosed):
                self._finish_stream(item.last_status)
            elif isinstance(item, _StreamFailed):
                self._fail_stream(item.error, item.last_status)
            elif isinstance(item, _NativeCompactResult):
                self._finish_native_compact(item)
            elif isinstance(item, _NativePlanResult):
                self._finish_native_plan(item)
            elif isinstance(item, _NativeReviewResult):
                self._finish_native_review(item)
            elif isinstance(item, _NativeShellResult):
                self._finish_native_shell(item)
            elif isinstance(item, _NativeBtwResult):
                self._finish_native_btw(item)
            elif isinstance(item, _NativeInfoResult):
                self._finish_native_info(item)

    def _consume_chunk(self, chunk: RuntimeStreamChunk, *, replay: bool = False) -> None:
        assert self._view is not None
        session_id = chunk.session.session.id
        if session_id != self._session_id:
            self._plan_mode = False
        self._session_id = session_id
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
            self._consume_output(chunk.output, replay=replay)

    def _consume_output(self, output: str, *, replay: bool = False) -> None:
        """The runtime publishes the complete answer after the provider deltas.

        Render it only when no delta was streamed for this attempt, or the answer
        would appear twice (the old app's ``_streamed_provider_text`` rule; the
        view owns the flag so a retraction resets it in one place).

        ``replay`` marks the chunk as history from the background poll. A poll
        repeats every second while a background task is tracked, and it re-sends
        the stored answer, so appending it would reprint the transcript once per
        second. Two rules keep a replay honest: it renders nothing when the tape
        already holds that text, and anything it does add is settled at once --
        an unsettled block would repeat on the next poll and grow
        ``Transcript.live_rows()`` without bound. (A replay can never be the first
        place an answer is seen: the poll only runs while this client is idle with
        a tracked task, a task is only tracked from an event on this client's own
        stream, and that stream has already consumed the turn's answer either as
        provider deltas or as the live ``output`` chunk.)
        """
        assert self._view is not None
        if self._view.streamed_provider_text:
            return
        if replay and any(isinstance(block, AssistantBlock) and block.text == output for block in self._view.transcript().blocks):
            return
        self._view.transcript().add(AssistantBlock(text=output, settled=replay))
        self._dirty = True

    def _consume_polled(self, item: _PolledChunks) -> None:
        if item.session_id != self._session_id:
            return
        for chunk in item.chunks:
            self._consume_chunk(chunk, replay=True)

    def _finish_stream(self, final_status: str) -> None:
        assert self._view is not None
        self._streaming = False
        self._cancel_requested = False
        self._view.finish_stream(final_status)
        self._take_pending_overlay()
        self._refresh_context_window()
        self._dirty = True

    def _fail_stream(self, error: object, last_status: str) -> None:
        assert self._view is not None
        self._streaming = False
        self._cancel_requested = False
        self._view.finish_stream(last_status, error=error)
        self._take_pending_overlay()
        self._refresh_context_window()
        self._dirty = True

    def _harvest_metadata(self, metadata: Mapping[str, object] | None) -> None:
        """Status-line data the runtime already delivers per chunk."""
        if not metadata:
            return
        collaboration_mode = metadata.get("collaboration_mode")
        if collaboration_mode in {"plan", "default"}:
            self._plan_mode = collaboration_mode == "plan"
        model, _provider = session_model_identity(metadata)
        if model:
            self._model = model
        effort = metadata.get("reasoning_effort")
        if isinstance(effort, str) and effort:
            self._session_effort = effort
        conversation = metadata.get("context_window")
        if isinstance(conversation, Mapping):
            after = conversation.get("usage_tokens_after")
            before = conversation.get("usage_tokens_before")
            self._context_tokens = (
                after
                if isinstance(after, int) and not isinstance(after, bool)
                else before
                if isinstance(before, int) and not isinstance(before, bool)
                else None
            )
        usage = metadata.get("provider_usage")
        if isinstance(usage, Mapping):
            cumulative = usage.get("cumulative")
            cost = cumulative.get("cost_usd") if isinstance(cumulative, Mapping) else None
            self._cost_usd = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        latest = usage.get("latest") if isinstance(usage, Mapping) else None
        if isinstance(usage, Mapping):
            token_fields = (
                ("input_tokens", "_input_tokens"),
                ("output_tokens", "_output_tokens"),
                ("cache_read_tokens", "_cache_read_tokens"),
                ("cache_write_tokens", "_cache_write_tokens"),
            )
            for key, attribute in token_fields:
                value = latest.get(key) if isinstance(latest, Mapping) else None
                setattr(self, attribute, value if isinstance(value, int) and not isinstance(value, bool) else None)
            hit_rate = latest.get("cache_hit_rate") if isinstance(latest, Mapping) else None
            self._cache_hit_rate = float(hit_rate) if isinstance(hit_rate, (int, float)) and not isinstance(hit_rate, bool) else None

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
        last_status = "unknown"
        saw_chunk = False
        try:
            for chunk in open_stream():
                saw_chunk = True
                last_status = chunk.session.status
                self._queue.put(chunk)
            if not saw_chunk:
                raise ValueError("runtime stream emitted no chunks")
        except Exception as error:
            self._queue.put(_StreamFailed(error, last_status))
            return
        self._queue.put(_StreamClosed(last_status))

    def _maybe_poll(self, now: float) -> None:
        """Replay the parent session's tail while idle with tracked background work.

        Background/delegated notices are persisted events on the *parent* session
        and the runtime has no subscription, so an idle client has to ask -- this
        is the old app's 1 s poll, ported. The view's sequence dedupe drops the
        events already applied; the replay's stored answer chunk is not an event,
        so ``_consume_output``'s replay rule keeps it from reprinting the
        transcript (see :meth:`TuiApp._consume_output`).
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
        self._expand_notice = ""
        if self._below_minimum_size():
            if key.name == "ctrl+c":
                self._quit = True
            elif key.name == "escape":
                if self._overlay is not None:
                    self._handle_overlay_key(key)
                elif self._composer is not None:
                    outcome = self._composer.handle_key(key)
                    if outcome.action is ComposerAction.CANCEL_TURN:
                        self._cancel_turn()
            return
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
        self._dirty = True

    def _run_action(self, action: str) -> None:
        handlers: Mapping[str, Callable[[], None]] = {
            "app.help": self._command_help,
            "app.tui.appearance": self._command_appearance,
            "app.tools.detail": self._command_tool_details,
            "app.tools.expand": self._toggle_expand,
            "app.session.new": self._command_session_new,
            "app.session.resume": self._command_session_resume,
            "app.session.tree": self._command_session_tree,
            "app.display.reset": self._command_display_reset,
            "app.history.search": self._command_history_search,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError(f"unhandled TUI action: {action}")
        handler()

    def _handle_overlay_key(self, key: Key) -> None:
        overlay = self._overlay
        assert overlay is not None
        if isinstance(overlay, QuestionOverlay) and key.name == "ctrl+c":
            outcome = OverlayOutcome(OverlayOutcomeKind.CANCELLED)
        else:
            outcome = overlay.handle_key(key)
        if isinstance(overlay, AppearanceOverlay):
            if outcome.kind is OverlayOutcomeKind.PENDING:
                self._preview_appearance(overlay.values)
            else:
                self._finish_appearance(outcome)
            self._dirty = True
            return
        if outcome.kind is OverlayOutcomeKind.PENDING:
            self._dirty = True
            return
        if isinstance(overlay, ToolArtifactOverlay):
            if outcome.kind is OverlayOutcomeKind.DONE and outcome.payload in {"next", "previous"}:
                if outcome.payload == "next" and self._artifact_next_offset is not None:
                    self._artifact_offsets.append(self._artifact_next_offset)
                elif outcome.payload == "previous" and len(self._artifact_offsets) > 1:
                    self._artifact_offsets.pop()
                self._open_tool_artifact_page(self._artifact_offsets[-1])
            else:
                self._close_overlay()
                self._artifact_detail = None
                self._artifact_offsets.clear()
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
            request_id = self._overlay_request_id
            self._close_overlay()
            if outcome.kind is OverlayOutcomeKind.CANCELLED:
                self._cancel_question(request_id)
            elif outcome.kind is OverlayOutcomeKind.DONE and isinstance(outcome.payload, tuple):
                self._answer_question(request_id, outcome.payload)
        elif isinstance(overlay, SessionPickerOverlay):
            call_id = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            tool_picker = self._tool_picker_active
            self._tool_picker_active = False
            self._close_overlay()
            if isinstance(call_id, str) and call_id:
                if tool_picker:
                    self._select_tool_artifact(call_id)
                else:
                    self._open_resumed_session(call_id)
        elif isinstance(overlay, TreeSelectorOverlay):
            sequence = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(sequence, int):
                self._checkout_session(sequence)
        elif isinstance(overlay, GoalConfirmOverlay):
            obj = overlay._objective
            self._close_overlay()
            if outcome.kind is OverlayOutcomeKind.DONE and outcome.payload == "replace":
                self._execute_set_goal(obj, mode="replace")
        elif isinstance(overlay, ReviewPresetPickerOverlay):
            preset = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if preset == "base_branch":
                branches = self._runtime.review_branches()
                current = self._runtime.review_current_branch()
                self._overlay = ReviewBranchPickerOverlay(branches=branches, current_branch=current, theme=self._theme)
                self._overlay_request_id = ""
                if self._composer is not None:
                    self._composer.set_enabled(False)
                self._dirty = True
            elif preset == "uncommitted":
                self._start_native_review(ReviewTarget(type="uncommitted"))
            elif preset == "commit":
                commits = self._runtime.review_recent_commits(limit=100)
                self._overlay = ReviewCommitPickerOverlay(commits=commits, theme=self._theme)
                self._overlay_request_id = ""
                if self._composer is not None:
                    self._composer.set_enabled(False)
                self._dirty = True
            elif preset == "custom":
                self._overlay = ReviewCustomPromptOverlay(theme=self._theme)
                self._overlay_request_id = ""
                if self._composer is not None:
                    self._composer.set_enabled(False)
                self._dirty = True
        elif isinstance(overlay, ReviewBranchPickerOverlay):
            branch = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(branch, str) and branch:
                self._start_native_review(ReviewTarget(type="base_branch", branch=branch))
        elif isinstance(overlay, ReviewCommitPickerOverlay):
            commit = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(commit, ReviewCommitLogEntry):
                self._start_native_review(ReviewTarget(type="commit", sha=commit.sha, title=commit.subject))
        elif isinstance(overlay, ReviewCustomPromptOverlay):
            instructions = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(instructions, str) and instructions:
                self._start_native_review(ReviewTarget(type="custom", instructions=instructions))
        elif isinstance(overlay, RenameSessionOverlay):
            name = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(name, str):
                if not name.strip():
                    self._notice("Thread name cannot be empty.")
                else:
                    self._apply_session_rename(name.strip())
        elif isinstance(overlay, CopyPickerOverlay):
            choice = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(choice, str):
                self._copy_choice(choice)
        elif isinstance(overlay, ExportPickerOverlay):
            destination = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if destination == "clipboard":
                self._copy_export_to_clipboard()
            elif destination == "file":
                self._open_export_path_overlay()
        elif isinstance(overlay, ExportPathOverlay):
            filename = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(filename, str) and filename:
                self._write_export_file(filename)
        else:
            prompt = outcome.payload if outcome.kind is OverlayOutcomeKind.DONE else None
            self._close_overlay()
            if isinstance(prompt, str) and prompt and self._composer is not None:
                self._composer.set_text(prompt)

    # -- flows -------------------------------------------------------------

    def _submit(self, text: str) -> None:
        prompt = text.strip()
        parts = prompt.split(maxsplit=1)
        command = parts[0] if parts else ""
        arguments = parts[1] if len(parts) > 1 else ""
        shell_command = parse_shell_escape(prompt)
        if shell_command is not None:
            # Native `!` escape runs before slash routing (even while
            # streaming: upstream ActiveTurnAuxiliary semantics — output only
            # lands in context, no turn lifecycle is started).
            self._command_native_shell(shell_command)
            return
        if command.startswith("/"):
            name = command[1:]
            native_cmd = NATIVE_COMMANDS_BY_NAME.get(name)
            if native_cmd is not None:
                if native_cmd.arg_rule == "none":
                    if arguments.strip():
                        self._notice(f"/{name} does not accept arguments")
                        return
                elif native_cmd.arg_rule == "enum":
                    if arguments.strip() not in native_cmd.allowed_args:
                        allowed = ", ".join(repr(a) for a in native_cmd.allowed_args if a)
                        self._notice(f"/{name} accepts only {allowed}")
                        return
                elif native_cmd.arg_rule == "required_text":
                    if not arguments.strip():
                        self._notice(f"/{name} requires arguments")
                        return

                if native_cmd.busy_gate and (self._streaming or self._plan_pending or self._review_pending or self._compact_pending):
                    if name in {"compact", "export", "plan", "review"}:
                        self._notice(f"✖ /{name} requires an idle active session")
                    else:
                        self._notice(f"'/{name}' is disabled while a task is in progress.")
                    return

                self._dispatch_native_command(name, arguments)
                return
        if not prompt:
            return
        if self._plan_pending or self._review_pending or self._compact_pending:
            self._notice("Wait for the current native command to finish")
            return
        if self._streaming or self._overlay is not None:
            self._steer(prompt)
            return
        self._start_prompt(prompt)

    def _dispatch_native_command(self, name: str, arguments: str) -> None:
        if name == "compact":
            self._command_native_compact()
        elif name == "diff":
            self._command_native_diff()
        elif name == "plan":
            self._command_native_plan(arguments.strip() == "off")
        elif name == "review":
            self._command_native_review(arguments)
        elif name == "goal":
            self._command_native_goal(arguments)
        elif name == "btw":
            self._command_native_btw(arguments)
        elif name == "init":
            self._command_native_init()
        elif name == "new":
            self._command_native_new(arguments)
        elif name == "clear":
            self._command_native_clear(arguments)
        elif name == "resume":
            self._command_native_resume(arguments)
        elif name == "fork":
            self._command_native_fork(arguments)
        elif name == "rename":
            self._command_native_rename(arguments)
        elif name == "status":
            self._command_native_status()
        elif name == "export":
            self._command_native_export(arguments)
        elif name == "copy":
            self._command_native_copy()
        elif name == "context":
            self._command_native_context(arguments.strip() == "all")

    def _command_native_init(self) -> None:
        self._start_prompt(INIT_PROMPT)

    def _command_native_compact(self) -> None:
        if self._streaming or self._session_id is None:
            self._notice("✖ /compact requires an idle active session")
            return
        if self._compact_pending:
            self._notice("/compact is already running")
            return
        session_id = self._session_id
        self._compact_pending = True
        self._notice("/compact: compacting context...")
        self._compact_thread = threading.Thread(
            target=self._compact_worker,
            args=(session_id,),
            name="voidcode-tui-compact",
            daemon=True,
        )
        self._compact_thread.start()

    def _compact_worker(self, session_id: str) -> None:
        try:
            result = self._runtime.compact_session(session_id=session_id)
        except Exception as error:
            self._queue.put(_NativeCompactResult(session_id=session_id, error=error))
        else:
            self._queue.put(_NativeCompactResult(session_id=session_id, result=result))

    def _finish_native_compact(self, item: _NativeCompactResult) -> None:
        self._compact_pending = False
        self._compact_thread = None
        if item.session_id != self._session_id:
            return
        if item.error is not None:
            self._notice(f"✖ /compact failed: {format_runtime_error(item.error)}")
            return
        result = item.result or {}
        if result.get("compacted") is True:
            self._refresh_context_window()
            self._notice("✓ Context compacted and saved")
        else:
            self._notice(f"/compact: {result.get('reason', 'no changes')}")

    def _command_native_diff(self) -> None:
        try:
            diff = self._runtime.review_diff_all()
        except Exception as error:
            self._notice(f"✖ /diff failed: {format_runtime_error(error)}")
            return
        if diff.state == "not_git_repo":
            self._notice("/diff: not a Git repository")
        elif diff.state == "clean":
            self._notice("/diff: no changes")
        elif diff.diff:
            assert self._view is not None
            self._view.transcript().add(DiffBlock(diff=diff.diff, path=diff.path, expanded=True))
        else:
            self._notice("/diff: no changes")
        self._dirty = True

    def _command_native_plan(self, off: bool) -> None:
        if self._streaming or self._session_id is None:
            self._notice("✖ /plan requires an idle active session")
            return
        if self._plan_pending or self._review_pending or self._compact_pending:
            self._notice("Wait for the current native command to finish")
            return
        session_id = self._session_id
        self._plan_pending = True
        self._plan_thread = threading.Thread(
            target=self._plan_worker,
            args=(session_id, "default" if off else "plan"),
            name="voidcode-tui-plan",
            daemon=True,
        )
        self._plan_thread.start()

    def _plan_worker(self, session_id: str, mode: str) -> None:
        try:
            result = self._runtime.set_collaboration_mode(session_id=session_id, mode=mode)
        except Exception as error:
            self._queue.put(_NativePlanResult(session_id=session_id, error=error))
        else:
            self._queue.put(_NativePlanResult(session_id=session_id, result=result))

    def _finish_native_plan(self, item: _NativePlanResult) -> None:
        self._plan_pending = False
        self._plan_thread = None
        if item.session_id != self._session_id:
            return
        if item.error is not None:
            self._notice(f"✖ /plan failed: {format_runtime_error(item.error)}")
            return
        mode = (item.result or {}).get("collaboration_mode")
        self._plan_mode = mode == "plan"
        self._notice("✓ Plan mode enabled (read-only)" if mode == "plan" else "✓ Plan mode disabled")

    def _command_native_goal(self, arguments: str) -> None:
        from ..runtime.goal import (
            GOAL_USAGE,
            ThreadGoal,
            continuation_prompt,
            goal_status_label,
            goal_usage_summary,
            objective_text_for_edit,
            should_confirm_before_replacing_goal,
        )

        args = arguments.strip()
        parts = args.split(maxsplit=1)
        subcommand = parts[0].lower() if parts else ""

        if not args:
            if self._session_id is None:
                self._notice(f"{GOAL_USAGE}\nThe session must start before you can change a goal.")
                return
            goal_data = self._runtime.get_goal(session_id=self._session_id)
            if goal_data is None:
                self._notice(f"{GOAL_USAGE}\nNo goal is currently set.")
                return
            goal = ThreadGoal.from_payload(goal_data)
            self._goal_status = goal.status
            self._notice(f"Goal {goal_status_label(goal.status)}\n{goal_usage_summary(goal)}")
            return

        if subcommand == "clear":
            if self._session_id is None:
                self._notice(f"{GOAL_USAGE}\nThe session must start before you can change a goal.")
                return
            cleared = self._runtime.clear_goal(session_id=self._session_id)
            if cleared:
                self._goal_status = None
                self._notice("Goal cleared")
            else:
                self._notice("No goal to clear\nThis thread does not currently have a goal.")
            return

        if subcommand == "pause":
            if self._session_id is None:
                self._notice(f"{GOAL_USAGE}\nThe session must start before you can change a goal.")
                return
            goal_data = self._runtime.set_goal(session_id=self._session_id, status="paused")
            goal = ThreadGoal.from_payload(goal_data)
            self._goal_status = goal.status
            self._notice(f"Goal {goal_status_label(goal.status)}\n{goal_usage_summary(goal)}")
            return

        if subcommand == "resume":
            if self._session_id is None:
                self._notice(f"{GOAL_USAGE}\nThe session must start before you can change a goal.")
                return
            goal_data = self._runtime.set_goal(session_id=self._session_id, status="active")
            goal = ThreadGoal.from_payload(goal_data)
            self._goal_status = goal.status
            self._notice(f"Goal {goal_status_label(goal.status)}\n{goal_usage_summary(goal)}")
            if not self._streaming:
                self._start_prompt(continuation_prompt(goal))
            return

        if subcommand == "edit":
            if self._session_id is None:
                self._notice(f"No goal is currently set.\n{GOAL_USAGE}\nCreate a goal before editing it.")
                return
            goal_data = self._runtime.get_goal(session_id=self._session_id)
            if goal_data is None:
                self._notice(f"No goal is currently set.\n{GOAL_USAGE}\nCreate a goal before editing it.")
                return
            raw_obj = objective_text_for_edit(self._workspace, str(goal_data.get("objective", "")))
            if self._composer is not None:
                self._composer.set_text(f"/goal {raw_obj}")
            return

        objective = args
        if not objective:
            self._notice("Goal objective must not be empty.")
            return
        if self._session_id is None:
            self._notice(f"{GOAL_USAGE}\nThe session must start before you can set a goal.")
            return

        existing = self._runtime.get_goal(session_id=self._session_id)
        if existing is not None and should_confirm_before_replacing_goal(ThreadGoal.from_payload(existing)):
            self._overlay = GoalConfirmOverlay(objective=objective, theme=self._theme)
            self._overlay_request_id = ""
            if self._composer is not None:
                self._composer.set_enabled(False)
            self._dirty = True
            return

        self._execute_set_goal(objective, mode="replace")

    def _execute_set_goal(self, objective: str, *, mode: str = "replace") -> None:
        from ..runtime.goal import (
            ThreadGoal,
            continuation_prompt,
            goal_status_label,
            goal_usage_summary,
        )

        if self._session_id is None:
            return
        try:
            goal_data = self._runtime.set_goal(
                session_id=self._session_id,
                objective=objective,
                status="active",
                mode=mode,
            )
        except Exception as error:
            self._notice(f"✖ /goal failed: {error}")
            return
        goal = ThreadGoal.from_payload(goal_data)
        self._goal_status = goal.status
        self._notice(f"Goal {goal_status_label(goal.status)}\n{goal_usage_summary(goal)}")
        if not self._streaming:
            self._start_prompt(continuation_prompt(goal))

    def _command_native_review(self, focus: str) -> None:
        if self._streaming or self._session_id is None:
            self._notice("✖ /review requires an idle active session")
            return
        if self._plan_pending or self._review_pending or self._compact_pending:
            self._notice("Wait for the current native command to finish")
            return
        if focus and focus.strip():
            self._start_native_review(ReviewTarget(type="custom", instructions=focus.strip()))
            return
        self._overlay = ReviewPresetPickerOverlay(theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._dirty = True

    def _start_native_review(self, target: ReviewTarget) -> None:
        session_id = self._session_id
        assert session_id is not None
        self._review_pending = True
        self._review_thread = threading.Thread(
            target=self._review_worker,
            args=(session_id, target),
            name="voidcode-tui-review",
            daemon=True,
        )
        self._review_thread.start()

    def _review_worker(self, session_id: str, target: ReviewTarget) -> None:
        try:
            response = self._runtime.review_session(session_id=session_id, target=target)
        except Exception as error:
            self._queue.put(_NativeReviewResult(session_id=session_id, error=error))
        else:
            self._queue.put(_NativeReviewResult(session_id=session_id, output=response.output))

    def _finish_native_review(self, item: _NativeReviewResult) -> None:
        self._review_pending = False
        self._review_thread = None
        if item.session_id != self._session_id:
            return
        if item.error is not None:
            self._notice(f"✖ /review failed: {format_runtime_error(item.error)}")
            return
        if not item.output:
            self._notice("/review completed without a summary")
            return
        assert self._view is not None
        self._view.transcript().add(AssistantBlock(text=item.output, settled=True))
        self._dirty = True

    def _command_native_shell(self, shell_command: str) -> None:
        # No idle gate and no pending gate: a `!` escape always executes,
        # even mid-turn and in plan mode (explicit user consent, not a
        # model tool call). The runtime persists history without starting
        # a provider turn.
        if not shell_command.strip():
            self._notice(f"{USER_SHELL_HELP_TITLE}\n{USER_SHELL_HELP_HINT}")
            return
        if self._overlay is not None:
            self._notice("✖ Cannot run a shell command while a dialog is open")
            return
        session_id = self._session_id
        threading.Thread(
            target=self._shell_worker,
            args=(session_id, shell_command),
            name="voidcode-tui-shell",
            daemon=True,
        ).start()

    def _shell_worker(self, session_id: str | None, shell_command: str) -> None:
        try:
            result = self._runtime.run_user_shell(session_id=session_id, command=shell_command)
        except Exception as error:
            self._queue.put(_NativeShellResult(session_id=session_id or "", error=error))
        else:
            self._queue.put(_NativeShellResult(session_id=str(result.get("session_id", session_id or "")), result=result))

    def _finish_native_shell(self, item: _NativeShellResult) -> None:
        if item.error is not None:
            self._notice(f"✖ ! shell command failed: {format_runtime_error(item.error)}")
            return
        result = item.result or {}
        if self._session_id is None and isinstance(result.get("session_id"), str) and result["session_id"]:
            self._session_id = str(result["session_id"])
        if item.session_id and self._session_id and item.session_id != self._session_id:
            return
        command = str(result.get("command", ""))
        output = str(result.get("output", ""))
        exit_code = result.get("exit_code", 0)
        status = str(result.get("status", "completed"))
        assert self._view is not None
        self._view.transcript().add(UserBlock(text=f"!{command}"))
        body = f"Exit code: {exit_code}\n{output}" if output else f"Exit code: {exit_code}"
        self._view.transcript().add(
            ToolBlock(
                tool="shell",
                title=f"!{command}",
                summary=f"exit {exit_code}",
                body=tuple(body.splitlines()),
                state="success" if status == "completed" else "error",
                settled=True,
            )
        )
        self._dirty = True

    def _command_native_btw(self, arguments: str) -> None:
        # immediate:true — no idle gate, no native pending gate: runs on a
        # worker thread even while the main session streams, and the answer
        # renders in an overlay without touching the transcript.
        if self._session_id is None:
            self._notice("Usage: /btw <your question>\nThe session must start before you can ask a side question.")
            return
        session_id = self._session_id
        question = arguments.strip()
        if not question:
            recent = self._btw_recent.get(session_id)
            if recent is None:
                self._notice("Usage: /btw <your question>")
                return
            self._open_btw_overlay(*recent)
            return
        if self._btw_pending:
            self._notice("/btw is already running")
            return
        self._btw_pending = True
        threading.Thread(
            target=self._btw_worker,
            args=(session_id, question),
            name="voidcode-tui-btw",
            daemon=True,
        ).start()

    def _btw_worker(self, session_id: str, question: str) -> None:
        try:
            result = self._runtime.ask_side_question(session_id=session_id, question=question)
        except Exception as error:
            self._queue.put(_NativeBtwResult(session_id=session_id, question=question, error=error))
        else:
            self._queue.put(
                _NativeBtwResult(session_id=session_id, question=str(result.get("question", question)), answer=str(result.get("answer", "")))
            )

    def _finish_native_btw(self, item: _NativeBtwResult) -> None:
        self._btw_pending = False
        if item.session_id != self._session_id:
            return
        if item.error is not None or not item.answer:
            self._notice(f"✖ /btw failed: {format_runtime_error(item.error) if item.error is not None else 'empty response'}")
            return
        self._btw_recent[item.session_id] = (item.question, item.answer)
        self._open_btw_overlay(item.question, item.answer)

    def _open_btw_overlay(self, question: str, answer: str) -> None:
        if self._overlay is not None:
            self._notice("✖ Cannot open /btw while a dialog is open")
            return
        self._overlay = SideQuestionOverlay(question=question, answer=answer, theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    # -- native read-only info commands (/status /export /copy /context) --------

    def _run_info_worker(self, kind: str, session_id: str, *args: object) -> None:
        if self._session_id is None:
            self._notice(f"✖ /{kind} requires an active session")
            return
        if kind in self._info_pending:
            self._notice(f"/{kind} is already running")
            return
        self._info_pending.add(kind)
        threading.Thread(target=self._info_worker, args=(kind, session_id, *args), name=f"voidcode-tui-{kind}", daemon=True).start()

    def _info_worker(self, kind: str, session_id: str, *args: object) -> None:
        try:
            if kind == "status":
                result = self._runtime.session_status_panel(session_id=session_id)
            elif kind == "export":
                result = self._runtime.export_session_markdown(session_id=session_id)
            elif kind == "copy":
                result = self._runtime.copy_targets(session_id=session_id)
            else:
                result = self._runtime.session_context_usage(session_id=session_id, expand_items=bool(args[0]) if args else False)
        except Exception as error:
            self._queue.put(_NativeInfoResult(session_id=session_id, kind=kind, error=error))
        else:
            self._queue.put(_NativeInfoResult(session_id=session_id, kind=kind, result=result))

    def _finish_native_info(self, item: _NativeInfoResult) -> None:
        self._info_pending.discard(item.kind)
        if item.session_id != self._session_id:
            return
        if item.error is not None:
            self._notice(f"✖ /{item.kind} failed: {format_runtime_error(item.error)}")
            return
        if item.kind == "status" and isinstance(item.result, SessionStatusPanel):
            self._render_status_panel(item.result)
        elif item.kind == "export" and isinstance(item.result, SessionExport):
            self._open_export_picker(item.result)
        elif item.kind == "copy" and isinstance(item.result, tuple):
            self._open_copy_picker(item.result)
        elif item.kind == "context" and isinstance(item.result, SessionContextUsage):
            self._render_context_usage(item.result)
        else:
            self._notice(f"✖ /{item.kind} failed: unexpected response")

    def _command_native_status(self) -> None:
        # No idle gate: read-only projection, safe mid-stream (Codex parity).
        if self._session_id is None:
            self._notice("✖ /status requires an active session")
            return
        self._run_info_worker("status", self._session_id)

    def _render_status_panel(self, panel: SessionStatusPanel | None) -> None:
        assert self._view is not None
        if panel is None:
            self._notice("✖ /status failed: empty response")
            return
        item = panel
        rows = [
            ("Session", f"{item.session_id}" + (f" ({item.title})" if item.title else "")),
            ("Status", f"{item.status} · turn {item.turn}"),
            ("Model", f"{item.provider}/{item.model}" if item.provider and item.model else (item.model or "unresolved")),
            ("Reasoning", item.reasoning_effort or "—"),
            ("Approval", item.approval_mode or "—"),
            ("Mode", item.collaboration_mode),
            ("Goal", f"{item.goal_status}: {item.goal_objective}" if item.goal_objective else "—"),
            (
                "Usage",
                f"in={item.input_tokens} out={item.output_tokens}" + (f" ${item.cost_usd:.4f}" if item.cost_usd is not None else "")
                if item.input_tokens is not None or item.output_tokens is not None
                else "—",
            ),
            ("Workspace", item.workspace or "—"),
            ("Endpoint", item.base_url or "—"),
        ]
        if item.forked_from_session_id:
            rows.append(("Forked from", item.forked_from_session_id))
        width = max(len(label) for label, _ in rows)
        lines = [f"{label:<{width}}  {value}" for label, value in rows]
        self._view.transcript().add(NoticeBlock(text="Status\n" + "\n".join(lines)))
        self._dirty = True

    def _command_native_export(self, arguments: str) -> None:
        # Idle-only gate (Codex blocks /export during a task).
        if self._streaming or self._session_id is None:
            self._notice("✖ /export requires an idle active session")
            return
        if self._plan_pending or self._review_pending or self._compact_pending:
            self._notice("Wait for the current native command to finish")
            return
        path = arguments.strip()
        if path:
            try:
                result = self._runtime.write_session_export(session_id=self._session_id, path=path)
            except Exception as error:
                self._notice(f"✖ /export failed: {format_runtime_error(error)}")
            else:
                self._notice(f"✓ Exported session to {result.get('path', path)}")
            return
        self._run_info_worker("export", self._session_id)

    def _open_export_picker(self, export: SessionExport | None) -> None:
        if export is None:
            self._notice("✖ /export failed: empty response")
            return
        self._export_markdown = export.markdown
        if self._overlay is not None:
            self._notice("✖ Cannot open /export while a dialog is open")
            return
        self._overlay = ExportPickerOverlay(theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._dirty = True

    def _copy_export_to_clipboard(self) -> None:
        try:
            copy_to_clipboard(self._export_markdown)
        except Exception as error:
            self._notice(f"✖ Copy failed: {error}")
        else:
            self._notice("Copied conversation to clipboard")

    def _open_export_path_overlay(self) -> None:
        default = f"voidcode-session-{self._session_id}.md" if self._session_id else "voidcode-session.md"
        self._overlay = ExportPathOverlay(initial_text=default, theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._dirty = True

    def _write_export_file(self, filename: str) -> None:
        if self._session_id is None:
            self._notice("✖ /export requires an active session")
            return
        try:
            result = self._runtime.write_session_export(session_id=self._session_id, path=filename)
        except Exception as error:
            self._notice(f"✖ /export failed: {format_runtime_error(error)}")
        else:
            self._notice(f"✓ Exported session to {result.get('path', filename)}")

    def _command_native_copy(self) -> None:
        # No idle gate: copies the last settled response, safe mid-stream.
        if self._session_id is None:
            self._notice("✖ /copy requires an active session")
            return
        self._run_info_worker("copy", self._session_id)

    def _open_copy_picker(self, targets: tuple[CopyTarget, ...] | None) -> None:
        items = list(targets) if targets else []
        if not items:
            self._notice("No agent response to copy")
            return
        if len(items) == 1:
            self._copy_text(items[0].text, items[0].label)
            return
        self._copy_targets = [(target.label, target.preview, target.text) for target in items]
        if self._overlay is not None:
            self._copy_text(items[0].text, items[0].label)
            return
        options = tuple((str(index), f"{label} — {preview}" if preview else label) for index, (label, preview, _) in enumerate(self._copy_targets))
        self._overlay = CopyPickerOverlay(options=options, theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._dirty = True

    def _copy_choice(self, choice: str) -> None:
        try:
            label, _, text = self._copy_targets[int(choice)]
        except ValueError, IndexError:
            self._notice("✖ Copy failed: unknown choice")
            return
        self._copy_text(text, label)

    def _copy_text(self, text: str, label: str) -> None:
        try:
            copy_to_clipboard(text)
        except Exception as error:
            self._notice(f"✖ Copy failed: {error}")
        else:
            self._notice(f"Copied {label} to clipboard")

    def _command_native_context(self, expand: bool) -> None:
        # No idle gate: read-only projection, safe mid-stream (Claude parity).
        if self._session_id is None:
            self._notice("✖ /context requires an active session")
            return
        self._run_info_worker("context", self._session_id, expand)

    def _render_context_usage(self, usage: SessionContextUsage | None) -> None:
        assert self._view is not None
        if usage is None:
            self._notice("✖ /context failed: empty response")
            return
        item = usage
        total = f" / {item.total_tokens}" if item.total_tokens else ""
        percent = f" ({item.percent}%)" if item.percent is not None else ""
        bar = self._usage_bar(item.percent)
        lines = [f"Context: {item.used_tokens}{total} tokens{percent} {bar}".rstrip()]
        if item.model:
            lines.append(f"Model: {item.model}")
        for segment in item.segments:
            lines.append(f"  {segment.label}: {segment.tokens}")
            for row in segment.items:
                lines.append(f"    {row.label}: {row.tokens}")
        if item.over_budget:
            lines.append("⚠ Over context budget — consider /compact")
        elif item.estimated:
            lines.append("(estimated — no provider usage anchor yet)")
        self._view.transcript().add(NoticeBlock(text="\n".join(lines)))
        self._dirty = True

    @staticmethod
    def _usage_bar(percent: float | None, width: int = 20) -> str:
        if percent is None:
            return ""
        filled = max(0, min(width, round(percent / 100 * width)))
        return "[" + "█" * filled + "░" * (width - filled) + "]"

    def _command_native_new(self, arguments: str) -> None:
        name = arguments.strip() if arguments else None
        assert self._view is not None
        self._session_id = None
        self._session_name = name
        self._approval_mode = self._config.approval_mode
        self._model = self._config.model or ""
        self._session_effort = ""
        self._cost_usd = None
        self._input_tokens = None
        self._output_tokens = None
        self._cache_read_tokens = None
        self._cache_write_tokens = None
        self._cache_hit_rate = None
        self._context_tokens = None
        self._tracked_tasks.clear()
        self._next_poll_at = 0.0
        label = f"New Session: {name}" if name else "New Session"
        self._view.transcript().add(SessionMarkerBlock(label=label))
        self._notice(f"✓ Started new session: {name}" if name else "✓ Started new session")
        self._dirty = True

    def _command_native_clear(self, arguments: str) -> None:
        name = arguments.strip() if arguments else None
        assert self._view is not None and self._region is not None
        self._region.clear()
        self._view.reset_for_new_session()
        self._session_id = None
        self._session_name = name
        self._approval_mode = self._config.approval_mode
        self._model = self._config.model or ""
        self._session_effort = ""
        self._cost_usd = None
        self._input_tokens = None
        self._output_tokens = None
        self._cache_read_tokens = None
        self._cache_write_tokens = None
        self._cache_hit_rate = None
        self._context_tokens = None
        self._tracked_tasks.clear()
        self._next_poll_at = 0.0
        label = f"New Session: {name}" if name else "New Session"
        self._view.transcript().add(SessionMarkerBlock(label=label))
        self._notice(f"✓ Cleared transcript; started new session: {name}" if name else "✓ Cleared transcript; started new session")
        self._dirty = True

    def _command_native_resume(self, arguments: str) -> None:
        id_or_name = arguments.strip()
        if not id_or_name:
            self._command_session_resume()
            return
        try:
            sessions = self._runtime.list_sessions()
        except Exception as error:
            self._notice(f"✖ Failed to list sessions: {format_runtime_error(error)}")
            return
        target_session_id: str | None = None
        target_title: str | None = None
        for s in sessions:
            if s.session.id == id_or_name or short_session_id(s.session.id) == id_or_name:
                target_session_id = s.session.id
                target_title = s.title
                break
        if target_session_id is None:
            for s in sessions:
                if s.title and s.title.casefold() == id_or_name.casefold():
                    target_session_id = s.session.id
                    target_title = s.title
                    break
        if target_session_id is None:
            self._notice(f"No saved chat found matching '{id_or_name}'.")
            return
        self._open_resumed_session(target_session_id)
        if target_title:
            self._session_name = target_title

    def _command_native_fork(self, arguments: str) -> None:
        if self._session_id is None:
            self._notice("✖ No active session to fork")
            return
        name = arguments.strip() if arguments else None
        session_id = self._session_id
        try:
            forked = self._runtime.fork_session(session_id=session_id)
        except Exception as error:
            self._notice(f"✖ /fork failed: {format_runtime_error(error)}")
            return
        if name:
            try:
                forked = self._runtime.rename_session(session_id=forked.session.id, title=name)
            except Exception as error:
                self._notice(f"Failed to name the forked session: {error}")
        self._open_resumed_session(forked.session.id)
        self._session_name = forked.title
        self._notice(f"✓ Forked session to {short_session_id(forked.session.id)}" + (f" ({name})" if name else ""))

    def _command_native_rename(self, arguments: str) -> None:
        if self._session_id is None:
            self._notice("The session must start before you can rename it.")
            return
        trimmed = arguments.strip()
        if not arguments:
            self._overlay = RenameSessionOverlay(initial_text=self._session_name or "", theme=self._theme)
            self._overlay_request_id = ""
            if self._composer is not None:
                self._composer.set_enabled(False)
            self._enter_alt_screen()
            self._dirty = True
            return
        if not trimmed:
            self._notice("Thread name cannot be empty.")
            return
        self._apply_session_rename(trimmed)

    def _apply_session_rename(self, name: str) -> None:
        assert self._session_id is not None
        try:
            updated = self._runtime.rename_session(session_id=self._session_id, title=name)
        except Exception as error:
            self._notice(f"✖ /rename failed: {format_runtime_error(error)}")
            return
        self._session_name = updated.title
        self._notice(f"✓ Session renamed to '{updated.title}'")
        self._dirty = True

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

    def _cancel_question(self, request_id: str) -> None:
        assert self._view is not None
        self._view.resolve_overlay(request_id)
        if self._session_id is None:
            self._notice("✖ Question cannot be cancelled without a session")
            return
        session_id = self._session_id
        self._start_stream(
            lambda: self._runtime.answer_question_stream(
                session_id=session_id,
                question_request_id=request_id,
                responses=QuestionCancellation(),
            )
        )
        self._dirty = True

    def _command_help(self) -> None:
        config = self._tui_config
        mode = config.theme.mode
        resolved = self._resolved_theme_mode
        settings = [f"Approval: {self._approval_mode}", f"Theme: {resolved}"]
        if mode == "auto":
            settings.append(f"Theme source: {self._auto_theme_source}" + ("; dark fallback" if self._auto_theme_fallback else ""))
        settings.extend(
            (
                f"Theme slots: dark={config.theme.dark} · light={config.theme.light}",
                f"Statusline: {config.statusline.layout}",
                f"Composer: {config.composer.style} · gutter={config.composer.gutter} · padding={config.composer.padding}",
                f"Cursor: {config.composer.cursor_shape} (blink={config.composer.cursor_blink})",
                "Native commands: /btw, /compact, /context, /copy, /diff, /export, /plan, /review, /status, !<shell command>",
                "Appearance: " + ("saved" if self._appearance_saved else "temporary session preview; not saved"),
            )
        )
        self._overlay = HelpOverlay(
            theme=self._theme,
            bindings={action: binding.name for action, binding in self._bindings.items()},
            settings=settings,
        )
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _command_appearance(self) -> None:
        config = self._tui_config
        theme = config.theme
        statusline = config.statusline
        composer = config.composer
        values = {
            "theme.mode": theme.mode,
            "theme.dark": theme.dark,
            "theme.light": theme.light,
            "statusline.layout": statusline.layout,
            "composer.style": composer.style,
            "composer.cursor": composer.cursor_shape,
        }
        choices = {
            "theme.mode": VALID_TUI_THEME_MODES,
            "theme.dark": palette_names("dark", theme.custom),
            "theme.light": palette_names("light", theme.custom),
            "statusline.layout": statusline_profiles(),
            "composer.style": VALID_TUI_COMPOSER_STYLES,
            "composer.cursor": VALID_TUI_COMPOSER_CURSOR_SHAPES,
        }
        self._appearance_original = config
        self._appearance_original_saved = self._appearance_saved
        assert self._theme is not None
        self._overlay = AppearanceOverlay(theme=self._theme, choices=choices, values=values)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _appearance_config(self, values: Mapping[str, str]) -> RuntimeTuiResolvedConfig:
        current = self._tui_config
        theme = replace(
            current.theme,
            mode=values["theme.mode"],
            dark=values["theme.dark"],
            light=values["theme.light"],
        )
        layout = values["statusline.layout"]
        if layout == "custom":
            if current.statusline.layout == "custom":
                left, right = current.statusline.left, current.statusline.right
            else:
                left, right = statusline_profile_segments(current.statusline.layout)
            statusline = replace(current.statusline, layout=layout, left=left, right=right)
        else:
            statusline = replace(current.statusline, layout=layout, left=None, right=None)
        composer = replace(
            current.composer,
            style=values["composer.style"],
            cursor_shape=values["composer.cursor"],
            cursor_blink=None if values["composer.cursor"] == "terminal" else current.composer.cursor_blink,
        )
        return replace(current, theme=theme, statusline=statusline, composer=composer)

    def _preview_appearance(self, values: Mapping[str, str]) -> bool:
        previous = self._tui_config
        config = self._appearance_config(values)
        try:
            self._apply_appearance_config(config)
        except TerminalCapabilityError as error:
            if isinstance(self._overlay, AppearanceOverlay):
                self._overlay.values.update(
                    {
                        "theme.mode": previous.theme.mode,
                        "theme.dark": previous.theme.dark,
                        "theme.light": previous.theme.light,
                        "statusline.layout": previous.statusline.layout,
                        "composer.style": previous.composer.style,
                        "composer.cursor": previous.composer.cursor_shape,
                    }
                )
                self._overlay.set_notice(f"Preview rolled back: {error}")
            self._dirty = True
            return False
        if config != previous:
            self._appearance_saved = False
        if isinstance(self._overlay, AppearanceOverlay):
            self._overlay.set_notice("")
        return True

    def _apply_appearance_config(self, config: RuntimeTuiResolvedConfig) -> None:
        assert self._term is not None
        self._validate_tui_appearance(config)
        self._term.set_cursor_shape(config.composer.cursor_shape, config.composer.cursor_blink)
        theme = self._resolve_theme(self._term, config.theme)
        self._tui_config = config
        self._theme = theme
        if self._view is not None:
            self._view.transcript().theme = theme
        if self._composer is not None:
            self._composer.set_appearance(
                theme=theme,
                style=config.composer.style,
                gutter=config.composer.gutter,
                padding=config.composer.padding,
            )
            self._composer.set_max_height(self._composer.input_height(self._term.height))
            status_width = self._composer.status_width()
        else:
            status_width = self._term.width
        self._status = StatusLine(theme, status_width, config.statusline)
        if isinstance(self._overlay, AppearanceOverlay):
            self._overlay.set_theme(theme)
        self._dirty = True

    def _finish_appearance(self, outcome: OverlayOutcome) -> None:
        payload = outcome.payload
        if outcome.kind is OverlayOutcomeKind.DONE and isinstance(payload, tuple):
            action = payload[0] if payload else None
            if action == "apply" and len(payload) == 2 and isinstance(payload[1], Mapping):
                if not self._preview_appearance(payload[1]):
                    return
                self._appearance_original = None
                self._appearance_original_saved = None
                self._close_overlay()
                return
            if action == "save" and len(payload) == 3 and payload[1] in {"user", "workspace"} and isinstance(payload[2], Mapping):
                if not self._preview_appearance(payload[2]):
                    return
                try:
                    latest = load_runtime_config(self._workspace)
                    self._validate_tui_appearance(latest.tui)
                    path = save_tui_appearance_config(payload[1], self._workspace, self._tui_config)
                except (OSError, ValueError) as error:
                    self._notice(f"Appearance not saved: {error}")
                    return
                self._notice(f"Appearance saved to {path}")
                self._appearance_saved = True
                self._appearance_original = None
                self._appearance_original_saved = None
                self._close_overlay()
                return
        if self._appearance_original is not None:
            self._apply_appearance_config(self._appearance_original)
        if self._appearance_original_saved is not None:
            self._appearance_saved = self._appearance_original_saved
        self._appearance_original = None
        self._appearance_original_saved = None
        self._close_overlay()

    def _command_tool_details(self) -> None:
        if self._view is None or self._session_id is None:
            self._notice("Tool output details require an active session.")
            return
        entries = [
            (block.activity_id, escape_terminal_controls(f"{block.title or block.tool}: {block.summary or 'output detail'}"))
            for block in self._view.transcript().blocks
            if isinstance(block, ToolBlock) and block.detail_available and block.tool_call_id and block.activity_id
        ]
        if not entries:
            self._notice("No tool output details are available in this transcript.")
            return
        self._tool_picker_active = True
        self._overlay = SessionPickerOverlay(
            sessions=entries,
            theme=self._theme,
            title="Select Tool Output",
            action="view",
        )
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _select_tool_artifact(self, activity_id: str) -> None:
        assert self._view is not None
        block = next(
            (item for item in self._view.transcript().blocks if isinstance(item, ToolBlock) and item.activity_id == activity_id),
            None,
        )
        if block is None or not block.detail_available:
            self._notice("Tool output detail is no longer available.")
            return
        session_id = block.session_id or self._session_id
        if not session_id:
            self._notice("Tool output has no session owner.")
            return
        self._artifact_detail = (session_id, block.tool_call_id, block.artifact_id, block.title or block.tool)
        self._artifact_offsets = [0]
        self._artifact_next_offset = None
        self._open_tool_artifact_page(0)
        self._enter_alt_screen()

    def _open_tool_artifact_page(self, offset: int) -> None:
        detail = self._artifact_detail
        caller = self._session_id
        if detail is None or caller is None:
            self._notice("Tool output detail requires an active session.")
            return
        session_id, tool_call_id, artifact_id, title = detail
        content = ""
        has_next = False
        self._artifact_next_offset = None
        limit = max(1, (self._term.height - 5) if self._term is not None else 35)
        try:
            access = self._runtime.read_tool_transcript(
                caller_session_id=caller,
                session_id=session_id,
                limit=1,
            )
            if not isinstance(access, TranscriptPage) or access.session_id != session_id:
                content = "Tool transcript is not accessible."
            else:
                result = self._runtime.read_tool_output_artifact(
                    caller_session_id=caller,
                    session_id=session_id,
                    artifact_id=artifact_id,
                    tool_call_id=tool_call_id,
                    offset=offset,
                    limit=limit,
                )
                if isinstance(result, ArtifactPage):
                    content = escape_terminal_controls(result.text, preserve_newlines=True)
                    if isinstance(result.page, NextPage):
                        has_next = True
                        self._artifact_next_offset = result.page.offset
                elif isinstance(result, ArtifactBinary):
                    content = (
                        f"Binary artifact · {escape_terminal_controls(result.mime)} · {result.byte_count} bytes "
                        f"· SHA-256 {escape_terminal_controls(result.sha256)}"
                    )
                elif isinstance(result, ArtifactInvalid | ArtifactUnavailable):
                    content = escape_terminal_controls(result.reason)
                elif isinstance(result, ArtifactMissing):
                    content = "Artifact is not available."
                else:
                    content = "Artifact read returned no displayable content."
        except Exception as error:
            logger.exception("Failed to read tool output artifact")
            content = escape_terminal_controls(format_runtime_error(error))
        self._overlay = ToolArtifactOverlay(
            title=escape_terminal_controls(title),
            content=content,
            page=len(self._artifact_offsets),
            has_previous=len(self._artifact_offsets) > 1,
            has_next=has_next,
            theme=self._theme,
        )
        self._overlay_request_id = ""
        self._dirty = True

    def _command_session_new(self) -> None:
        assert self._view is not None and self._region is not None
        self._region.clear()
        self._view.reset_for_new_session()
        self._view.transcript().add(SessionMarkerBlock(label="New Session"))
        self._session_id = None
        self._session_name = None
        self._approval_mode = self._config.approval_mode
        self._model = self._config.model or ""
        self._session_effort = ""
        self._cost_usd = None
        self._input_tokens = None
        self._output_tokens = None
        self._cache_read_tokens = None
        self._cache_write_tokens = None
        self._cache_hit_rate = None
        self._context_tokens = None
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

        # Main-session surface, like ``sessions list``/HTTP: delegated
        # background-task children are reachable only from the child-session
        # view, so they never appear as resume targets here.
        rows_by_id = {
            summary.session.id: (
                summary.session.id,
                summary.title if summary.title is not None else summary.prompt,
            )
            for summary in sessions
            if summary.session.parent_id is None
        }
        # Row order and display depth both come from the runtime's forest -- the
        # one projection every tree surface renders -- so the picker never sorts
        # by ``updated_at`` itself: doing that put a continued fork above the
        # root it branched from. A session the forest does not know about (a read
        # that raced a brand-new row) still renders, flat at depth 0, at the end.
        try:
            forest = self._runtime.session_forest()
        except Exception as error:
            logger.error("Failed to read session forest: %s", error)
            self._notice(f"✖ Failed to read the session tree: {format_runtime_error(error)}")
            return
        entries = [rows_by_id.pop(entry.session_id) for entry in forest if entry.session_id in rows_by_id]
        entries.extend(rows_by_id.values())
        depths = {entry.session_id: entry.depth for entry in forest}
        self._overlay = SessionPickerOverlay(sessions=entries, depths=depths, theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _command_session_tree(self) -> None:
        """``app.session.tree``: pick an entry of the current session to continue from.

        The runtime owns the tree; this only gathers its entries (ascending
        sequence -- the selector renders them in exactly that order, so a row
        can never appear above its parent) and hands the chosen sequence to
        :meth:`_checkout_session`.
        """
        if self._session_id is None:
            self._notice("✖ No active session to check out")
            return
        try:
            entries = self._runtime.session_entries(self._session_id)
        except Exception as error:
            logger.error("Failed to read session entries: %s", error)
            self._notice(f"✖ Failed to read the session tree: {format_runtime_error(error)}")
            return
        self._overlay = TreeSelectorOverlay(entries=entries, theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _checkout_session(self, sequence: int) -> None:
        """Move the session's leaf; the abandoned turns stay in the tape, marked.

        A refused checkout (a mid-tool sequence, a session that moved under us)
        is a notice, not a crash: nothing was written, so the tape is unchanged.
        """
        if self._session_id is None:
            return
        assert self._view is not None
        try:
            leaf = self._runtime.checkout_session(self._session_id, sequence)
            entries = self._runtime.session_entries(self._session_id)
        except Exception as error:
            logger.error("Checkout failed: %s", error)
            self._notice(f"✖ Checkout failed: {format_runtime_error(error)}")
            return
        # Everything after the new leaf is off the current path; those turns are
        # retained in the session but no longer reach the model. Counting by
        # sequence (not `not on_current_path`) keeps an earlier-abandoned branch
        # out of the number -- the path ends at the leaf, so any later entry is
        # exactly one of the turns this checkout parked.
        abandoned = sum(1 for entry in entries if entry.sequence > leaf)
        self._view.transcript().add(SessionMarkerBlock(label=f"Checked out at #{leaf}", hint=f"{abandoned} later turns retained, off-path"))
        self._dirty = True

    def _command_display_reset(self) -> None:
        """``app.display.reset``: force a full repaint of the live region.

        Committed rows live in native scrollback and are never rewritten, so the
        reset only drops the terminal's paint cache: the next frame is emitted in
        full instead of being diffed against the previous one. It repairs a live
        region the terminal corrupted; it cannot recover lost scrollback.
        """
        if self._term is not None:
            self._term.reset_live()
        self._dirty = True

    def _command_history_search(self) -> None:
        """``app.history.search``: pick a previous prompt back into the draft.

        The composer already keeps the bounded prompt history, so an empty one
        means there is nothing to search.
        """
        history = self._composer.history if self._composer is not None else ()
        if not history:
            self._notice("✖ No prompt history to search")
            return
        self._overlay = PromptHistoryOverlay(entries=history, theme=self._theme)
        self._overlay_request_id = ""
        if self._composer is not None:
            self._composer.set_enabled(False)
        self._enter_alt_screen()
        self._dirty = True

    def _open_resumed_session(self, session_id: str) -> None:
        assert self._view is not None and self._region is not None
        self._region.clear()
        self._view.reset_for_new_session()
        self._session_id = session_id
        self._session_name = None
        try:
            for s in self._runtime.list_sessions():
                if s.session.id == session_id:
                    self._session_name = s.title
                    break
        except Exception:
            pass
        try:
            config = self._runtime.run_config(session_id=session_id)
            self._approval_mode = config.approval_mode
        except (TypeError, ValueError) as error:
            logger.error("Failed to resolve session approval mode: %s", error)
            self._approval_mode = "unavailable"
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

    def _toggle_expand(self) -> None:
        """``app.tools.expand``: expand or collapse every block.

        Settled transcript rows already in native scrollback cannot be redrawn:
        rewriting them would duplicate history. ``Transcript.set_expanded``
        reports whether a toggle reached only committed blocks, and the app
        shows that limitation only when no live tail remains. The notice rides
        the live region, so the gesture adds no scrollback rows.
        """
        assert self._view is not None
        transcript = self._view.transcript()
        if not transcript.blocks:
            return
        expanded = not all(block.expanded for block in transcript.blocks)
        label = "enabled" if expanded else "disabled"
        committed_only = transcript.set_expanded(expanded) and not transcript.live_rows()
        self._expand_notice = (
            f"Tool output expansion {label} (committed rows cannot be redrawn)" if committed_only else f"Tool output expansion {label}"
        )
        self._dirty = True

    def _notice(self, text: str) -> None:
        assert self._view is not None
        self._view.transcript().add(NoticeBlock(text=text))
        self._dirty = True

    # -- overlays ----------------------------------------------------------

    def _take_pending_overlay(self) -> None:
        assert self._view is not None
        if self._overlay is not None or self._streaming:
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
        compact_thread = self._compact_thread
        if compact_thread is not None and compact_thread.is_alive():
            compact_thread.join(_SHUTDOWN_JOIN_SECONDS)
        for native_thread in (self._plan_thread, self._review_thread):
            if native_thread is not None and native_thread.is_alive():
                native_thread.join(_SHUTDOWN_JOIN_SECONDS)
        try:
            self._runtime.__exit__(None, None, None)
        except Exception as error:
            logger.error("Failed to shut down runtime: %s", error)
        finally:
            terminal.close()


def run_tui(
    *,
    workspace: Path,
    approval_mode: ApprovalMode | None = None,
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
    except TuiConfigurationError as error:
        print(f"voidcode tui: {error}", file=sys.stderr)
        return 2
    except KeyBindingError as error:
        print(f"voidcode tui: {error}", file=sys.stderr)
        return 2
    try:
        return app.run()
    except TerminalCapabilityError as error:
        print(f"voidcode tui: {error}", file=sys.stderr)
        return 2
