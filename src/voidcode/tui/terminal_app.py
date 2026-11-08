"""Interactive inline TUI application for the VoidCode core transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Mapping
from typing import Any, Literal

from voidcode.capabilities.providers.native.reasoning_effort import normalize_reasoning_effort
from voidcode.config import BannerConfig
from voidcode.core.events import EventType
from voidcode.sdk.client import (
    CommandCatalog,
    CommandResult,
    CoreClientError,
    ErrorFrame,
    EventFrame,
    HeadFrame,
    HealthStatus,
    RestartFrame,
    ResumeSpec,
    RunSpec,
    SessionClient,
    StreamCancellation,
    StreamEvent,
)
from voidcode.sdk.codec import decode_tool_call, encode_tool_result
from voidcode.sdk.commands import CommandDescriptor, ConfirmationRequest
from voidcode.sdk.gates import (
    ActivationCommitted,
    ApprovalGate,
    ApprovalRequested,
    ConfirmationGate,
    ConfirmationRequested,
    GateChange,
    GateDecided,
    GateState,
    RunTerminated,
    reduce_gates,
)
from voidcode.sdk.icons import Icon
from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.tools import (
    ToolEffect,
)
from voidcode.sdk.transcript import (
    TranscriptChild,
    TranscriptContent,
    TranscriptDispatch,
    TranscriptInterrupted,
    TranscriptRun,
    TranscriptTool,
)
from voidcode.sdk.views import (
    ChoiceOption,
    ChoicesViewData,
    ConfirmationViewData,
    MarkdownViewData,
    PluginViewDescriptor,
    is_read_only_load,
    parse_view_data,
)

from .composer import Composer, ComposerAction
from .keys import Key, KeyDecoder
from .region import TuiFrame
from .styles import SPINNER_FRAMES, Styles
from .term import Terminal, clamp_row, crop_row_right, escape_terminal_controls, visible_width
from .views import (
    InteractiveApproval,
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    InteractiveMarkdown,
    render_assistant_markdown,
    render_plugin_view,
    render_thinking,
    render_tool_call,
    render_tool_result,
    render_user_message,
)

logger = logging.getLogger(__name__)

#: Control bytes inside model/plugin text must never reach the terminal as
#: commands (they could clear scrollback or switch buffers), so they are made
#: visible before the row is styled or committed.

#: A lone ESC byte is ambiguous (it may start a sequence) until no further input
#: follows: the decoder buffers it, and the loop resolves it after this window.
#: Matches the 50 ms window the pre-cutover client used.
_ESCAPE_FLUSH_SECONDS = 0.05

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


def _banner_rows(banner: BannerConfig | None, styles: Styles, *, width: int) -> list[str]:
    """Centered welcome mark rows, or nothing when disabled/widthless."""
    if banner is None or width <= 0:
        return []
    raw = _BUILTIN_BANNER if banner.text is None else tuple(banner.text.split("\n"))
    cleaned = [escape_terminal_controls(line.rstrip(), preserve_tabs=False) for line in raw]
    if not any(line.strip() for line in cleaned):
        return []
    colors = banner.colors
    content_width = min(width, max(visible_width(line) for line in cleaned))
    pad = " " * ((width - content_width) // 2)
    rows: list[str] = []
    for index, line in enumerate(cleaned):
        clipped = clamp_row(line, width)
        color = colors[index * (len(colors) - 1) // max(1, len(cleaned) - 1)]
        rows.append(pad + styles.fg("accent", clipped, color=color))
    return rows


def _safe(value: object) -> str:
    """One-line, control-free rendering of any plugin- or model-provided value."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return escape_terminal_controls(text, preserve_tabs=False)


def json_safe(value: object) -> object:
    """Round-trip unknown payload pieces through JSON so anything is serializable."""
    from voidcode.sdk.json_values import json_wire_value

    try:
        return json.loads(json.dumps(json_wire_value(value), ensure_ascii=False, allow_nan=False, default=str))
    except TypeError, ValueError:
        return str(value)


def _prefixed_rows(prefix: str, text: str, styles: Styles, *, slot: str) -> list[str]:
    """Prefix sanitized multiline text, one row per explicit line.

    Terminal owns physical width wrapping, so this helper never clips content.
    """
    label = f"{styles.render(slot, prefix, bold=True)} "
    clean = escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True)
    rows: list[str] = []
    for index, line in enumerate(clean.split("\n")):
        head = label if index == 0 else " " * len(prefix) + " "
        rows.append(f"{head}{line}")
    return rows or [label]


def _command_result_text(result: CommandResult) -> str:
    """One command result's human text: error, then output, then pretty data.

    ``CommandResult`` carries both human text (``output``) and machine data
    (``data``). The TUI renders the human text when present and falls back to
    JSON for data-only receipts (for example the core ``show`` command). The
    fallback preserves an empty mapping and nested falsy values because it
    serializes the value itself, never a truthiness test.
    """
    if result.status == "error" and isinstance(result.error, str) and result.error:
        return result.error
    if isinstance(result.output, str) and result.output:
        return result.output
    if result.data is not None:
        return json.dumps(json_safe(result.data), indent=2, ensure_ascii=False)
    return ""


def _slash_public_path(descriptor: CommandDescriptor) -> str:
    """One public slash path per catalog entry: preferred slash syntax, else dots-to-spaces."""
    syntax = descriptor.client_syntax
    if isinstance(syntax, str) and syntax.startswith("/"):
        path = syntax[1:].strip().replace(".", " ")
        if path:
            return path
    return descriptor.name.replace(".", " ")


def _slash_display(name: str) -> str:
    """Human slash spelling for a machine command id (result rows, notices, status)."""
    return name.replace(".", " ")


def _command_result_rows(name: str, result: CommandResult, styles: Styles, width: int = 80) -> list[str]:
    """Render direct receipts and durable command results consistently."""
    label = _safe(_slash_display(name) or "command")
    text = _command_result_text(result)
    if result.status == "error":
        return _prefixed_rows(f"Error: /{label}:", text or "Command failed", styles, slot="error")
    if result.status == "pending_confirmation":
        return [styles.fg("warning", f"Confirmation required: /{label}")]
    if result.status == "ok":
        presentation = result.presentation
        if presentation is not None and presentation.view_type not in ("choices", "form"):
            return render_plugin_view(
                PluginViewDescriptor(owner="command", name=name, title=presentation.title, view_type=presentation.view_type, data=presentation.data),
                width,
                styles,
            )
        return escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True).split("\n") if text else []
    return _prefixed_rows(f"Warning: /{label} ({result.status or 'unknown'}):", text, styles, slot="warning")


#: App/editor keys a catalog shortcut may never claim; the client keeps them.
#: Compared case-insensitively: catalog chords are lowercase (``alt+pageup``)
#: while the decoder names keys like ``pageUp``.
_RESERVED_SHORTCUTS: frozenset[str] = frozenset(
    {
        "escape",
        "esc",
        "enter",
        "tab",
        "shift+tab",
        "space",
        "backspace",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "ctrl+c",
        "ctrl+d",
        "ctrl+s",
        "ctrl+enter",
        "shift+enter",
        "ctrl+j",
        "alt+enter",
        "alt+left",
        "alt+right",
        "alt+b",
        "alt+f",
        "ctrl+left",
        "ctrl+right",
        "ctrl+w",
        "alt+backspace",
        "ctrl+backspace",
        "alt+delete",
        "alt+d",
    }
)


def _normalize_chord(name: str) -> str:
    """Case-insensitive chord form so ``alt+pageUp`` matches ``alt+pageup``."""
    return name.strip().lower()


def _shortcut_bindings(descriptors: tuple[CommandDescriptor, ...]) -> tuple[dict[str, CommandDescriptor], dict[str, tuple[str, ...]]]:
    """Map one chord to one descriptor; a chord with 2+ claimants binds nobody.

    Reserved app/editor keys never bind. Returns ``(bindings, conflicts)`` where
    conflicts names every claimant so the client can say so explicitly.
    """
    claimants: dict[str, list[CommandDescriptor]] = {}
    for descriptor in descriptors:
        chord = descriptor.default_shortcut
        if not chord:
            continue
        normalized = _normalize_chord(chord)
        if normalized in _RESERVED_SHORTCUTS:
            continue
        claimants.setdefault(normalized, []).append(descriptor)
    bindings: dict[str, CommandDescriptor] = {}
    conflicts: dict[str, tuple[str, ...]] = {}
    for chord, entries in claimants.items():
        if len(entries) == 1:
            bindings[chord] = entries[0]
        else:
            conflicts[chord] = tuple(entry.name for entry in entries)
    return bindings, conflicts


def _content_rows(
    content: TranscriptContent,
    width: int,
    styles: Styles,
    *,
    include_tool_header: bool = True,
) -> list[str]:
    """Render one typed transcript content into settled rows."""
    if isinstance(content, ProviderMessage):
        if content.role == "user":
            return render_user_message(content.content or "", width, styles) if content.content and content.content.strip() else []
        if content.role == "system":
            return []
        rows: list[str] = []
        if content.content:
            rows.extend(render_assistant_markdown(content.content, width, styles))
        return rows
    if isinstance(content, ProviderTurnResult):
        rows = []
        if isinstance(content.reasoning, str) and content.reasoning:
            rows.extend(render_thinking(content.reasoning, width, styles))
        if isinstance(content.output, str) and content.output:
            rows.extend(render_assistant_markdown(content.output, width, styles))
        return rows
    if isinstance(content, TranscriptTool):
        rows = []
        if include_tool_header:
            rows.extend(
                render_tool_call(
                    _safe(content.call.tool_name or "tool"),
                    json_safe(content.call.arguments),
                    width,
                    styles,
                    icon=content.icon,
                )
            )
        rows.extend(render_tool_result(encode_tool_result(content.result), width, styles, interrupted=content.interrupted))
        return rows
    if isinstance(content, TranscriptRun):
        if content.status == "failed":
            return [styles.fg("error", "Error: Run failed")]
        if content.status == "interrupted":
            return [styles.fg("dim", "Interrupted")]
        return []
    if isinstance(content, TranscriptInterrupted):
        return []  # The terminal run event owns the human outcome; /usage keeps accounting.
    if isinstance(content, TranscriptChild):
        return []
    if isinstance(content, TranscriptDispatch):
        return []
    raise ValueError(f"unknown transcript content: {type(content).__name__}")


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


def _gate_changes(event: StreamEvent) -> tuple[list[GateChange], bool]:
    """Decode one stream event's gate changes plus whether the run terminated.

    Returns ``(changes, run_terminated)``; the caller folds changes through
    the reducer and applies the same gate UI rules as before.
    """
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    if event.type == EventType.CONFIRMATION_REQUESTED:
        if str(payload.get("ui_type", "")) != "confirmation":
            return [], False
        request = ConfirmationRequest.from_payload(payload)
        return [ConfirmationRequested(ConfirmationGate.from_request(request, revision_id=event.revision_id))], False
    if event.type == EventType.CONFIRMATION_DECIDED:
        request_id = str(payload.get("request_id") or "")
        return ([GateDecided(request_id)] if request_id else []), False
    if event.type == EventType.APPROVAL_REQUESTED:
        request_id = payload.get("request_id")
        raw_call = payload.get("call")
        if not isinstance(request_id, str) or not request_id or not isinstance(raw_call, Mapping):
            return [], False
        gate = _approval_gate_from_payload(payload, run_id=event.run_id, revision_id=event.revision_id)
        return [ApprovalRequested(gate)], False
    if event.type == EventType.APPROVAL_DECIDED:
        request_id = str(payload.get("request_id") or "")
        return ([GateDecided(request_id)] if request_id else []), False
    if event.type == EventType.ACTIVATION_COMMITTED:
        return [ActivationCommitted(str(payload.get("to_revision_id") or ""))], False
    if event.type in (EventType.RUN_INTERRUPTED, EventType.RUN_FAILED):
        return [RunTerminated(event.run_id)], True
    if event.type == EventType.RUN_COMPLETED:
        return [RunTerminated(event.run_id)], False
    return [], False


def _tool_icon(raw: object) -> Icon | None:
    """Strict birth icon: None stays generic, malformed fails visibly."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"tool icon must be an object or null, got {type(raw).__name__}")
    glyph = raw.get("glyph")
    if not isinstance(glyph, str):
        raise ValueError("tool icon requires a glyph string")
    return Icon(glyph=glyph)


def _notice_row(text: object, tone: str, styles: Styles) -> str:
    slot = {"ok": "success", "warning": "warning", "error": "error"}.get(tone, "dim")
    prefix = {"warning": "Warning: ", "error": "Error: "}.get(tone, "")
    return styles.fg(slot, f"{prefix}{_safe(text)}")


def _model_segment(model: object) -> str:
    """`provider/model`, elif raw, else `no model` — from the frozen health.model DTO only."""
    if model is None:
        return "no model"
    raw = getattr(model, "raw", None)
    provider = getattr(model, "provider", None)
    name = getattr(model, "model", None)
    if isinstance(provider, str) and provider and isinstance(name, str) and name:
        return f"{provider}/{name}"
    if isinstance(raw, str) and raw:
        return raw
    return "no model"


def _effort_segment(model: object) -> str | None:
    """reasoning_effort verbatim; None hides the segment (no marker)."""
    effort = getattr(model, "reasoning_effort", None)
    return effort if isinstance(effort, str) and effort else None


def _levels_text(model: object) -> str:
    """Supported levels verbatim, generic over the DTO; empty/unknown stays honest."""
    levels = getattr(model, "reasoning_levels", None)
    if isinstance(levels, (list, tuple)) and levels and all(isinstance(entry, str) for entry in levels):
        return ", ".join(levels)
    return "unknown"


def _short_workspace(path: str, *, local: bool) -> str:
    """`~`-shorten only when the path is known local; server paths stay full (crop handles width)."""
    if not local:
        return path
    home = os.path.expanduser("~")
    if home and home != "~" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path


def _footer_row(workspace: str, model: str, effort: str | None, session: str, *, width: int, compact: bool) -> str:
    """Context row with width degrade: crop workspace → drop session → drop workspace → model·effort → model."""
    sep = " · "
    if compact:
        return clamp_row(sep.join([model] + ([effort] if effort else [])), width)
    tail = [model] + ([effort] if effort else [])
    for drop_session in (False, True):
        segments = ([workspace] if drop_session or not session else [workspace, session]) + tail if workspace else tail
        text = sep.join(s for s in segments if s)
        if visible_width(text) <= width:
            return text
        if workspace and visible_width(workspace) > 8:
            rest = sep.join(s for s in segments[1:] if s)
            room = max(0, width - visible_width(sep + rest) if rest else width)
            if room >= 8:
                return crop_row_right(workspace, room) + (sep + rest if rest else "")
    text = sep.join(tail)
    if visible_width(text) <= width:
        return text
    return clamp_row(model, width)


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
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._session_cursors: dict[str, int] = {}
        #: Invocation ids whose tool-call header already committed (dedupes intent/result).
        self._tool_headers: set[str] = set()
        self._generation = 0
        #: Last known server identity/liveness from /api/health. The fence bit
        #: pauses dispatch; the identity triple detects a replaced server.
        self.upgrade: HealthStatus | None = None
        #: False until the current generation's stream delivers its first frame;
        #: the stream worker rearms it on every reconnect so each established
        #: stream reconciles exactly once (announced restart or silent drop).
        self._stream_ready = False
        #: Monotonic serial: only the newest reconcile round may render, so two
        #: overlapping health reads cannot reorder (generation alone is not enough).
        self._reconcile_serial = 0
        #: Highest reconcile serial already applied; older replies are dropped.
        self._reconcile_applied = 0
        #: Request ids with a worker in flight (approval/confirmation/invoke):
        #: a held Enter/click while one is outstanding is ignored, never a
        #: second thread. Cleared on response, decided fact, or error.
        self._in_flight: set[str] = set()
        self._interaction_serial = 0
        self.active_overlay: InteractiveChoices | InteractiveConfirmation | InteractiveForm | InteractiveMarkdown | None = None
        #: Chord -> descriptor for this catalog revision, rebuilt on every catalog load.
        self._shortcut_bindings: dict[str, CommandDescriptor] = {}
        #: Chord -> claimant names when 2+ descriptors claim one chord (binds nobody).
        self._shortcut_conflicts: dict[str, tuple[str, ...]] = {}
        #: Live read-only input source while a refreshable view is open; only this
        #: explicit metadata enables polling: {name, arguments, revision, source}.
        self._live_input: dict[str, Any] | None = None
        #: Next monotonic deadline for the live-input refresh worker.
        self._live_refresh_at: float = 0.0
        #: Serial of the latest live refresh: stale replies never touch the view.
        self._live_serial: int = 0
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
        self.status_line = "Connecting to server..."
        self._stream_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        #: Owns the current generation's session event connection so quit can wake a blocked idle read
        #: before joining. A replacement stream closes this handle and installs its own; workers
        #: capture their handle locally so a late detach from an old generation cannot clear it.
        self._stream_cancel: StreamCancellation | None = None
        #: Sessions whose welcome mark already committed; each empty visit shows it once.
        self._welcomed_sessions: set[str] = set()

    def run(self) -> int:
        self.running = True
        try:
            self._init_session()
            self._load_catalog()
            self._maybe_welcome()
            self._start_event_stream()
            return self._main_loop()
        finally:
            self.running = False
            self._stop_event.set()
            # Cancel the stream before joining the worker: an idle follow stream is
            # waiting on async network events, and task cancellation wakes it immediately.
            try:
                if self._stream_cancel is not None:
                    self._stream_cancel.close()
            finally:
                if self._stream_thread and self._stream_thread.is_alive():
                    self._stream_thread.join(timeout=1.0)
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

    def _cache_catalog(self, commands: tuple[CommandDescriptor, ...], revision: str) -> None:
        """Store one catalog revision and rebuild its cached shortcut bindings."""
        self.catalog_cache = commands
        self.catalog_revision = revision
        self._shortcut_bindings, self._shortcut_conflicts = _shortcut_bindings(commands)
        if self.composer.completion:
            self._sync_slash_completion()

    def _load_catalog(self) -> None:
        try:
            catalog = self.client.command_catalog()
            self._cache_catalog(catalog.commands, catalog.revision_id)
        except Exception as exc:
            self.catalog_cache = ()
            self.catalog_revision = ""
            self._shortcut_bindings, self._shortcut_conflicts = {}, {}
            self.status_line = f"Catalog load failed: {_safe(exc)}"
            raise RuntimeError(f"Failed to fetch command catalog from server: {exc}") from exc

    def _start_event_stream(self) -> None:
        generation = self._generation
        session_id = self.session_id
        previous, self._stream_cancel = self._stream_cancel, StreamCancellation()
        if previous is not None:
            previous.close()
        cancel = self._stream_cancel

        async def stream_worker() -> None:
            first_connection = True
            while not self._stop_event.is_set() and generation == self._generation:
                after_seq = self._session_cursors.get(session_id, 0)
                if not first_connection:
                    self._stream_ready = False
                first_connection = False
                try:
                    async with contextlib.aclosing(
                        self.client.events_stream(session_id, after_sequence=after_seq, follow=True, cancel=cancel)
                    ) as stream:
                        async for frame in stream:
                            if self._stop_event.is_set() or generation != self._generation:
                                return
                            if isinstance(frame, HeadFrame):
                                self.event_queue.put(
                                    {
                                        "kind": "stream_head",
                                        "frame": frame,
                                        "session_id": session_id,
                                        "generation": generation,
                                    }
                                )
                            elif isinstance(frame, EventFrame):
                                self.event_queue.put({"kind": "stream_event", "frame": frame, "session_id": session_id, "generation": generation})
                            elif isinstance(frame, RestartFrame):
                                self.event_queue.put({"kind": "server_restart", "session_id": session_id, "generation": generation})
                                break
                            elif isinstance(frame, ErrorFrame):
                                self.event_queue.put({"kind": "error", "error": frame.error, "session_id": session_id, "generation": generation})
                                return
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    if self._stop_event.is_set() or generation != self._generation:
                        return
                    if isinstance(exc, CoreClientError) and exc.status_code in {400, 401, 403, 404}:
                        self.event_queue.put({"kind": "error", "error": str(exc), "session_id": session_id, "generation": generation})
                        return
                    logger.debug("session event stream reconnecting after: %s", exc)
                    await asyncio.sleep(1.0)

        def run_worker() -> None:
            asyncio.run(stream_worker())

        self._stream_thread = threading.Thread(target=run_worker, daemon=True, name=f"CoreTuiStream{generation}")
        self._stream_thread.start()

    def _main_loop(self) -> int:
        while self.running:
            self._drain_events()
            self._process_input()
            self._poll_live_input()
            self._render_frame()
            time.sleep(0.033)  # ~30 fps redraw loop
        return 0

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

    def _footer_context(self, width: int) -> str:
        """Context row from the admitted health DTO only: workspace · model · effort."""
        if self.footer_mode == "off":
            return ""
        from voidcode.tui.local_client import CoreLocalClient

        model = getattr(self.upgrade, "model", None)
        local = isinstance(self.client, CoreLocalClient)
        workspace = _short_workspace(self.upgrade.workspace, local=local) if self.upgrade is not None and self.upgrade.workspace else ""
        session = self.session_id[:8] if self.show_session and self.session_id else ""
        return _footer_row(workspace, _model_segment(model), _effort_segment(model), session, width=width, compact=self.footer_mode == "compact")

    def _border_status(self) -> str:
        """`<spinner> working` while a run is active; idle borders stay clean."""
        if not self._run_active:
            return ""
        spinner = SPINNER_FRAMES[self._spinner_tick % len(SPINNER_FRAMES)] if SPINNER_FRAMES else ""
        label = "cancelling" if self._cancel_pending else "working · Esc to interrupt"
        return f"{spinner} {label}" if spinner else label

    def _drain_events(self) -> None:
        rows_to_commit: list[str] = []
        while True:
            try:
                frame = self.event_queue.get_nowait()
            except queue.Empty:
                break

            frame_generation = frame.get("generation")
            frame_session = frame.get("session_id")
            if isinstance(frame_generation, int) and frame_generation != self._generation:
                continue  # a worker from a previous session: never touch this UI
            if isinstance(frame_session, str) and frame_session != self.session_id:
                continue  # a result for another session can never commit rows here

            kind = frame.get("kind")
            if not self._stream_ready and kind in ("stream_head", "stream_event", "server_restart", "error"):
                # A frame on a not-ready stream means a (re)connection was just
                # established: run the one reconciliation path. The worker
                # rearms the flag on every reconnect, so announced restarts and
                # silent drops share this code path — no per-frame re-trigger.
                self._stream_ready = True
                self._reconcile_connection_async()
            if kind == "stream_head":
                head = frame.get("frame")
                if isinstance(head, HeadFrame):
                    if head.events:
                        self._welcomed_sessions.add(self.session_id)
                    for event in head.events:
                        self._handle_stream_event(event, rows_to_commit)
            elif kind == "stream_event":
                head = frame.get("frame")
                if isinstance(head, EventFrame):
                    self._welcomed_sessions.add(self.session_id)
                    self._handle_stream_event(head.event, rows_to_commit)
            elif kind == "notice":
                rows_to_commit.append(_notice_row(frame.get("text"), str(frame.get("tone", "info")), self.styles))
            elif kind == "command_done":
                raw_name = str(frame.get("name") or "command")
                status = str(frame.get("status", ""))
                # A real pending gate and a fenced core own the status line.
                # Never replace "approval required"/"confirmation required"/
                # "upgrade in progress" with a late completion notice.
                fenced = self.upgrade is not None and self.upgrade.fenced
                gate_open = self.pending_approval is not None or isinstance(self.active_overlay, InteractiveConfirmation)
                if status in ("ok", "error") and not fenced and not gate_open:
                    self.status_line = ""
                receipt = frame.get("result")
                if isinstance(receipt, CommandResult):
                    rows_to_commit.extend(_command_result_rows(raw_name, receipt, self.styles, self.terminal.width))
            elif kind == "command_input_ready":
                if frame.get("serial") != self._interaction_serial or frame.get("revision_id") != self.catalog_revision:
                    continue
                if self.status_line.startswith("Loading "):
                    self.status_line = ""
                # The composer draft is intentionally retained under an input
                # view (Alt+A never clears it); the serial already invalidates a
                # later edit, so only a gate/overlay blocks the mount.
                if self.active_overlay is not None or self.pending_approval is not None:
                    continue
                if self.upgrade is not None and self.upgrade.fenced:
                    continue
                error = frame.get("error")
                view = frame.get("view")
                if error:
                    self._live_input = None
                    self._live_refresh_at = 0.0
                    rows_to_commit.append(self.styles.fg("error", f"Command input unavailable: {_safe(error)}"))
                elif isinstance(view, PluginViewDescriptor):
                    refresh_after_ms = frame.get("refresh_after_ms")
                    if not isinstance(refresh_after_ms, int):
                        refresh_after_ms = None
                    if refresh_after_ms is not None:
                        action = str(frame.get("action", "")) or view.name
                        arguments = frame.get("arguments")
                        if not self._mount_live_view(
                            view,
                            self.catalog_revision,
                            name=action,
                            arguments=arguments if isinstance(arguments, dict) else {},
                            refresh_after_ms=refresh_after_ms,
                        ):
                            self._close_live_input()
                            rows_to_commit.append(
                                self.styles.fg("warning", f"Live view {action or 'unavailable'} returned an unsupported type; refresh paused")
                            )
                    elif not self._mount_interactive_view(view, self.catalog_revision):
                        rows_to_commit.extend(render_plugin_view(view, self.terminal.width, self.styles))
            elif kind == "live_input_ready":
                if frame.get("serial") != self._live_serial:
                    continue
                live = self._live_input
                if live is None or not isinstance(self.active_overlay, (InteractiveChoices, InteractiveMarkdown)):
                    continue
                if live.get("source") is not self.active_overlay:
                    continue
                same_action = str(frame.get("action", "")) == str(live.get("name", ""))
                same_args = dict(frame.get("arguments") or {}) == dict(live.get("arguments") or {})
                if not (same_action and same_args):
                    continue
                revision = str(frame.get("revision_id", ""))
                if revision != self.catalog_revision or revision != str(live.get("revision", "")):
                    continue
                error = frame.get("error")
                view = frame.get("view")
                refresh_after_ms = frame.get("refresh_after_ms")
                if not isinstance(refresh_after_ms, int):
                    refresh_after_ms = None
                cadence = live.get("refresh_after_ms")
                if not isinstance(cadence, int):
                    cadence = refresh_after_ms
                if error or not isinstance(view, PluginViewDescriptor):
                    self.status_line = f"Refresh failed: {_safe(error or 'unavailable')} · reopen to retry"
                    if isinstance(cadence, int):
                        self._live_refresh_at = time.monotonic() + cadence / 1000.0
                    continue
                try:
                    data = parse_view_data(view.view_type, view.data)
                except Exception as exc:
                    self.status_line = f"Refresh failed: {_safe(exc)} · reopen to retry"
                    if isinstance(cadence, int):
                        self._live_refresh_at = time.monotonic() + cadence / 1000.0
                    continue
                if view.view_type == "choices" and isinstance(self.active_overlay, InteractiveChoices):
                    from voidcode.sdk.views import ChoicesViewData

                    if not isinstance(data, ChoicesViewData):
                        continue
                    self.active_overlay.update_view(data)
                    self.active_overlay.revision_id = revision
                    self.active_overlay.title = view.title
                elif view.view_type == "markdown" and isinstance(self.active_overlay, InteractiveMarkdown):
                    if not isinstance(data, MarkdownViewData):
                        continue
                    previous_scroll = self.active_overlay.scroll
                    self.active_overlay.view = data
                    self.active_overlay.revision_id = revision
                    self.active_overlay.title = view.title
                    self.active_overlay.selected_index = min(self.active_overlay.selected_index, max(0, len(data.actions) - 1))
                    self.active_overlay.scroll = previous_scroll
                else:
                    rows_to_commit.append(self.styles.fg("warning", f"Live view returned {view.view_type}; refresh paused until reopened"))
                    self._close_live_input()
                    continue
                self._arm_live_input(
                    name=str(live.get("name", "")),
                    arguments=dict(live.get("arguments") or {}),
                    revision=revision,
                    refresh_after_ms=refresh_after_ms if refresh_after_ms is not None else cadence,
                    source=self.active_overlay,
                )
            elif kind == "command_presentation":
                if frame.get("serial") != self._interaction_serial:
                    continue
                view = frame.get("view")
                revision = str(frame.get("revision_id", ""))
                if isinstance(view, PluginViewDescriptor) and revision == self.catalog_revision:
                    if self.pending_approval is None and self.active_overlay is None:
                        self._mount_interactive_view(view, revision)
            elif kind == "approval_accepted":
                # The server accepted this exact request: close the gate it
                # matches, or nothing when a newer request already replaced it.
                accepted = str(frame.get("request_id", ""))
                self._in_flight.discard(accepted)
                if accepted:
                    self._apply_gate_change(GateDecided(accepted))
                self.status_line = ""
            elif kind == "decision_done":
                # A failed submission releases its in-flight marker so the user
                # can retry; the gate/overlay itself stays mounted.
                self._in_flight.discard(str(frame.get("request_id", "")))
            elif kind == "run_status":
                status = str(frame.get("status", ""))
                # Returned runs have durable terminal events; only failures without
                # a run id need local rows. This preserves transcript order.
                if status in ("failed", "interrupted") and not frame.get("run_id"):
                    self._render_run_notice(status, frame.get("error"), rows_to_commit)
                if status in ("completed", "failed", "interrupted"):
                    self._run_active = False
                    self._cancel_pending = False
                    self.status_line = ""
                elif status in ("running", "waiting"):
                    self._run_active = True
                    self.status_line = ""
            elif kind == "cancel_done":
                self._cancel_pending = False
                self.status_line = ""
            elif kind == "run_rejected":
                self._run_active = False
                self._cancel_pending = False
                if not self.composer.value:
                    self.composer.set_text(str(frame.get("prompt") or ""))
            elif kind == "run_waiting":
                gate = frame.get("gate")
                if isinstance(gate, ApprovalGate):
                    self._apply_gate_change(ApprovalRequested(gate))
            elif kind == "confirmation":
                confirmation = frame.get("confirmation")
                if isinstance(confirmation, ConfirmationRequest):
                    # Bound to the revision the client invoked under, never to
                    # whatever happens to be current when the user decides.
                    gate = ConfirmationGate.from_request(confirmation, revision_id=str(frame.get("revision_id", "")))
                    self._apply_gate_change(ConfirmationRequested(gate))
            elif kind == "confirmation_accepted":
                # The server accepted this exact request: close the overlay it
                # matches, or nothing when a newer request already replaced it.
                # A decided id never resurrects: the durable decided fact (or
                # the reducer tombstone) already closed it permanently.
                accepted = str(frame.get("request_id", ""))
                self._in_flight.discard(accepted)
                if accepted:
                    self._apply_gate_change(GateDecided(accepted))
                self.status_line = ""
            elif kind == "invoke_accepted":
                # The server accepted the submitting overlay's action: close it,
                # but only when it is still the overlay that submitted (same
                # action + same displayed revision). A 503 never emits this, so
                # the draft survives a fenced refusal untouched.
                submit_key = str(frame.get("request_id", ""))
                self._in_flight.discard(submit_key)
                overlay = self.active_overlay
                action = str(frame.get("action", ""))
                revision = str(frame.get("revision_id", ""))
                if (
                    submit_key
                    and isinstance(overlay, (InteractiveChoices, InteractiveForm, InteractiveMarkdown))
                    and submit_key == f"invoke:{action}@{revision}"
                    and overlay.revision_id == revision
                    and overlay is frame.get("overlay")
                ):
                    self._close_live_input()
                    self.active_overlay = None
                    self._present_queued_confirmation()
            elif kind == "form_load":
                form = frame.get("form")
                if isinstance(form, InteractiveForm) and form is self.active_overlay:
                    data = frame.get("data")
                    if isinstance(data, dict):
                        form.populate_from_load_action(data)
                    error = frame.get("error")
                    if error:
                        form.load_error = str(error)
            elif kind == "health":
                snapshot = frame.get("health")
                serial = frame.get("reconcile_serial")
                # Latest serial wins: an older round is dropped even when a
                # newer round has not applied yet (>= would let the older win
                # the race). Health and reconciled are two envelopes of ONE
                # round sharing its serial, so == matches both.
                if (
                    isinstance(snapshot, HealthStatus)
                    and isinstance(serial, int)
                    and serial == self._reconcile_serial
                    and serial > self._reconcile_applied
                ):
                    self._reconcile_applied = serial
                    self.upgrade = snapshot
                    # A cosmetic refresh never overwrites a live gate prompt: the
                    # approval/confirmation text owns the notice row until decided.
                    gate_open = self.pending_approval is not None or isinstance(self.active_overlay, InteractiveConfirmation)
                    if snapshot.fenced:
                        self.status_line = "Core upgrade in progress: dispatch paused"
                    elif not gate_open:
                        self.status_line = ""
            elif kind == "reconciled":
                serial = frame.get("reconcile_serial")
                if isinstance(serial, int) and serial == self._reconcile_serial and serial >= self._reconcile_applied:
                    # Catalog and session status from the same round as the
                    # health read above: applied together or not at all.
                    # Optional views are not part of this round; only the
                    # explicit /tui views control fetches them. Failures are
                    # surfaced, never swallowed into None: unknown stays
                    # visible while last-known state is preserved.
                    self._reconcile_applied = serial
                    catalog = frame.get("catalog")
                    failures = frame.get("failures")
                    if isinstance(catalog, CommandCatalog):
                        self._cache_catalog(catalog.commands, catalog.revision_id)
                    status = frame.get("session_status")
                    if isinstance(status, str) and status:
                        self.status_line = f"Run {status}" if status in ("running", "waiting") else self.status_line
                    if isinstance(failures, list) and failures:
                        rows_to_commit.append(self.styles.fg("warning", f"[Reconnect refresh incomplete: {', '.join(str(f) for f in failures)}]"))
            elif kind == "server_restart":
                # Watermark only (contract §8.2/§6): the worker already broke
                # its iterator and is reconnecting from our applied cursor.
                # Never set _session_cursors from the frame: queued but
                # uncommitted events would be dropped. Reconciliation runs on
                # the reconnected stream (first-frame trigger above), so an
                # announced restart and a silent drop share one path — and no
                # blocking request ever runs on this UI thread.
                self.status_line = "Reconnecting…"
            elif kind == "error":
                rows_to_commit.extend(_prefixed_rows("Error: event stream:", str(frame.get("error") or "Stream stopped"), self.styles, slot="error"))

        if rows_to_commit:
            self.terminal.commit_rows(rows_to_commit)

    def _handle_stream_event(self, event: StreamEvent, rows: list[str]) -> None:
        committed = self._session_cursors.get(self.session_id, 0)
        if event.sequence <= committed:
            return  # Settled ledger: never replay already committed events
        self._session_cursors[self.session_id] = event.sequence
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        etype = event.type
        entry = event.transcript
        if (
            entry is not None
            and entry.sequence == event.sequence
            and etype
            not in (
                EventType.RUN_COMPLETED,
                EventType.RUN_FAILED,
                EventType.RUN_INTERRUPTED,
            )
        ):
            if isinstance(entry.content, ProviderTurnResult):
                self._settle_stream(payload)
            include_header = True
            if isinstance(entry.content, TranscriptTool):
                # The live header (TOOL_INTENT) and this settled block share one
                # invocation id: pop it so the header commits exactly once.
                invocation = payload.get("invocation_id")
                if isinstance(invocation, str) and invocation:
                    include_header = invocation not in self._tool_headers
                    self._tool_headers.discard(invocation)
            rows.extend(_content_rows(entry.content, self.terminal.width, self.styles, include_tool_header=include_header))
        if etype in (EventType.USER_MESSAGE, EventType.RUN_RESUMED):
            self._run_active = True
            self.status_line = ""
        elif etype == EventType.TOOL_INTENT:
            invocation = payload.get("invocation_id")
            raw_call = payload.get("call")
            if isinstance(invocation, str) and invocation and isinstance(raw_call, Mapping):
                call = decode_tool_call(raw_call)
                icon = _tool_icon(payload.get("icon"))
                if invocation not in self._tool_headers:
                    self._tool_headers.add(invocation)
                    rows.extend(
                        render_tool_call(
                            _safe(call.tool_name or "tool"),
                            json_safe(call.arguments),
                            self.terminal.width,
                            self.styles,
                            icon=icon,
                        )
                    )
        elif etype == EventType.PROVIDER_PROGRESS:
            dispatch = payload.get("dispatch_id")
            delta = payload.get("text_delta")
            if isinstance(dispatch, str) and dispatch and isinstance(delta, str) and delta:
                channel = str(payload.get("channel", "text"))
                target = self._stream_reasoning if channel == "reasoning" else self._stream_buffers
                target.setdefault(dispatch, []).append(delta)
                self._stream_dispatch = dispatch
                self._run_active = True
        elif etype == EventType.COMMAND_COMPLETED:
            raw_result = payload.get("result")
            raw_name = str(payload.get("name") or "command")
            if isinstance(raw_result, Mapping):
                rows.extend(_command_result_rows(raw_name, CommandResult.from_payload(raw_result), self.styles, self.terminal.width))
        elif etype == EventType.ACTIVATION_COMMITTED:
            # The displayed generation changed: the command catalog is
            # refetched and anything captured under the previous revision is
            # dropped — never relabelled, never retried. Optional views are
            # explicit-only, so activation never fetches or renders them.
            to_revision_id = str(payload.get("to_revision_id") or "")
            overlay = self.active_overlay
            dropped = overlay is not None and getattr(overlay, "revision_id", "") != to_revision_id
            if dropped:
                self._close_live_input()
                self.active_overlay = None
            self._apply_gate_change(ActivationCommitted(to_revision_id))
            self._load_catalog()
            self._refresh_health_async()
            self._present_queued_confirmation()
            if dropped:
                rows.append(self.styles.fg("warning", "Warning: pending action discarded after configuration changed"))
        elif etype in (EventType.CONFIRMATION_REQUESTED, EventType.CONFIRMATION_DECIDED, EventType.APPROVAL_REQUESTED, EventType.APPROVAL_DECIDED):
            for change in _gate_changes(event)[0]:
                self._apply_gate_change(change)
        elif etype == EventType.RUN_INTERRUPTED:
            self._render_run_notice("interrupted", payload.get("reason"), rows, stop_cause=payload.get("stop_cause"))
            for change in _gate_changes(event)[0]:
                self._apply_gate_change(change)
            self._clear_stream()
            self._present_queued_confirmation()
        elif etype == EventType.RUN_FAILED:
            self._render_run_notice("failed", payload.get("error"), rows)
            for change in _gate_changes(event)[0]:
                self._apply_gate_change(change)
            self._clear_stream()
            self._present_queued_confirmation()
        elif etype == EventType.RUN_COMPLETED:
            for change in _gate_changes(event)[0]:
                self._apply_gate_change(change)
            self._clear_stream()
        elif etype == EventType.OBSERVER_ERROR:
            rows.extend(_prefixed_rows("Warning: plugin observer:", str(payload.get("error") or "Observer failed"), self.styles, slot="warning"))

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

    def _clear_stream(self) -> None:
        self._stream_buffers.clear()
        self._stream_reasoning.clear()
        self._stream_dispatch = None
        self._run_active = False
        self._cancel_pending = False
        self.status_line = ""

    def _close_live_input(self) -> None:
        """Stop polling; the open overlay (if any) is the caller's to clear."""
        self._live_input = None
        self._live_refresh_at = 0.0
        self._live_serial += 1
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
            self._live_serial += 1
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

    def _process_input(self) -> None:
        now = time.monotonic()
        chunk = self.terminal.read_bytes(timeout=0.001)
        if self.terminal.eof:
            self.running = False
            return
        if chunk:
            # A lone ESC is ambiguous until no further bytes follow: the standard
            # disambiguation window resolves it as Esc.
            self._escape_flush_at = now + _ESCAPE_FLUSH_SECONDS
            keys = list(self.key_decoder.feed(chunk))
        elif self._escape_flush_at and now >= self._escape_flush_at:
            self._escape_flush_at = 0.0
            keys = self.key_decoder.flush()
        else:
            keys = []
        for key in keys:
            if key.name == "escape" and (self.pending_approval is not None or self.active_overlay is not None):
                # The decoder names the raw byte "escape"; the overlays speak
                # "esc". The composer keeps the decoder's own name.
                key = Key("esc")
            if key.name == "ctrl+c":
                now = time.monotonic()
                if now - self._quit_armed_at < 1.5:
                    self.running = False
                    return
                self._quit_armed_at = now
                self.composer.clear()
                continue
            self._quit_armed_at = float("-inf")
            if key.name == "ctrl+d" and not self.composer.value and self.pending_approval is None and self.active_overlay is None:
                self.running = False
                return

            if self.pending_approval is not None:
                self._handle_approval_key(key)
            elif self.active_overlay is not None:
                self._handle_overlay_key(key)
            else:
                self._handle_composer_key(key)

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
                    self.event_queue.put({"kind": "cancel_done", "session_id": session_id, "generation": generation})
            except Exception as exc:
                self._queue_notice(f"Cancel error: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put({"kind": "cancel_done", "session_id": session_id, "generation": generation})

        threading.Thread(target=worker, daemon=True, name="CoreCancel").start()

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
                res = self.client.resume(session_id, ResumeSpec(approval_request_id=request_id, decision=decision))
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    # Still armed with the same request id: no auto-resend, the
                    # user re-decides after the restart.
                    self._upgrade_pending_notice(f"approval {decision}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"Approval {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put({"kind": "decision_done", "request_id": request_id, "session_id": session_id, "generation": generation})
                return
            except Exception as exc:
                self._queue_notice(f"Approval {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put({"kind": "decision_done", "request_id": request_id, "session_id": session_id, "generation": generation})
                return
            status = res.status
            self.event_queue.put(
                {
                    "kind": "approval_accepted",
                    "request_id": request_id,
                    "decision": decision,
                    "status": status,
                    "session_id": session_id,
                    "generation": generation,
                }
            )

        threading.Thread(target=worker, daemon=True, name="CoreApproval").start()

    def _handle_overlay_key(self, key: Any) -> None:
        assert self.active_overlay is not None
        outcome = self.active_overlay.handle_key(key)
        if outcome is None:
            return

        if outcome.get("cancel"):
            self._interaction_serial += 1
            self._close_live_input()
            self.active_overlay = None
            self.status_line = ""
            self._present_queued_confirmation()
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
                descriptor = next((item for item in self._catalog() if item.name == action), None)
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
                res = self.client.confirm(
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
                self.event_queue.put({"kind": "decision_done", "request_id": request_id, "session_id": session_id, "generation": generation})
                return
            except Exception as exc:
                self._queue_notice(f"Confirmation {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                self.event_queue.put({"kind": "decision_done", "request_id": request_id, "session_id": session_id, "generation": generation})
                return
            status = res.status
            self.event_queue.put(
                {
                    "kind": "confirmation_accepted",
                    "request_id": request_id,
                    "decision": decision,
                    "status": status,
                    "session_id": session_id,
                    "generation": generation,
                }
            )

        threading.Thread(target=worker, daemon=True, name="CoreConfirm").start()

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
        self._interaction_serial += 1
        serial = self._interaction_serial
        submitted_overlay = self.active_overlay
        shown = display_name if display_name else _slash_display(name)
        session_id, generation = self.session_id, self._generation

        def finish(status: str, result: CommandResult | None = None) -> None:
            """Hand this command's captured completion to the UI thread once."""
            self.event_queue.put(
                {
                    "kind": "command_done",
                    "name": name,
                    "status": status,
                    "result": result,
                    "session_id": session_id,
                    "generation": generation,
                }
            )

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
                    self.event_queue.put({"kind": "decision_done", "request_id": submit_key, "session_id": session_id, "generation": generation})
                return
            except Exception as exc:
                self._queue_notice(f"/{shown} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                finish("error")
                if submit_key is not None:
                    self.event_queue.put({"kind": "decision_done", "request_id": submit_key, "session_id": session_id, "generation": generation})
                return
            status = res.status
            if status == "pending_confirmation":
                confirmation = res.confirmation
                if confirmation is not None:
                    self.event_queue.put(
                        {
                            "kind": "confirmation",
                            "confirmation": confirmation,
                            "revision_id": revision,
                            "session_id": session_id,
                            "generation": generation,
                        }
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
                self.event_queue.put(
                    {
                        "kind": "decision_done" if status == "error" else "invoke_accepted",
                        "overlay": submitted_overlay,
                        "request_id": submit_key,
                        "action": name,
                        "revision_id": revision,
                        "session_id": session_id,
                        "generation": generation,
                    }
                )

            if res.status == "ok" and res.presentation is not None:
                presentation = res.presentation
                self.event_queue.put(
                    {
                        "kind": "command_presentation",
                        "serial": serial,
                        "view": PluginViewDescriptor(
                            owner="command", name=name, title=presentation.title, view_type=presentation.view_type, data=presentation.data
                        ),
                        "revision_id": revision,
                        "session_id": session_id,
                        "generation": generation,
                    }
                )

        threading.Thread(target=worker, daemon=True, name="CoreInvoke").start()

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
            {
                "kind": "notice",
                "text": text,
                "tone": tone,
                "session_id": self.session_id if session_id is None else session_id,
                "generation": self._generation if generation is None else generation,
            }
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
        self._reconcile_serial += 1
        serial = self._reconcile_serial
        bound_session = self.session_id if session_id is None else session_id
        bound_generation = self._generation if generation is None else generation

        def worker() -> None:
            try:
                snapshot = self.client.health()
            except Exception:
                return  # unreachable during a handoff gap is expected; keep last known
            self.event_queue.put(
                {
                    "kind": "health",
                    "health": snapshot,
                    "reconcile_serial": serial,
                    "session_id": bound_session,
                    "generation": bound_generation,
                }
            )

        threading.Thread(target=worker, daemon=True, name="CoreHealth").start()

    def _reconcile_connection_async(self) -> None:
        """ONE connection-established reconciliation path, entirely off the UI thread.

        Runs on the first frame of every (re)connected stream — announced restart
        or silent drop. Fetches health + catalog + durable session status in one
        worker round, then hands the UI thread a single serial-tagged envelope.
        Optional views are never fetched here: only explicit ``/tui views``
        presents them. Only the newest round renders (generation alone cannot
        order overlapping replies). Never resends the prompt, never touches the
        applied cursor.
        """
        self._reconcile_serial += 1
        serial = self._reconcile_serial
        session_id, generation = self.session_id, self._generation

        def worker() -> None:
            try:
                health = self.client.health()
            except Exception:
                return  # handoff gap: keep last known identity, stay silent
            failures: list[str] = []
            try:
                catalog = self.client.command_catalog()
            except Exception as exc:
                catalog = None
                failures.append(f"catalog ({exc})")
            try:
                session_status = self.client.get_session(session_id).status
            except Exception as exc:
                session_status = ""
                failures.append(f"session status ({exc})")
            self.event_queue.put(
                {
                    "kind": "health",
                    "health": health,
                    "reconcile_serial": serial,
                    "session_id": session_id,
                    "generation": generation,
                }
            )
            self.event_queue.put(
                {
                    "kind": "reconciled",
                    "catalog": catalog,
                    "session_status": session_status,
                    "failures": failures,
                    "reconcile_serial": serial,
                    "session_id": session_id,
                    "generation": generation,
                }
            )

        threading.Thread(target=worker, daemon=True, name="CoreReconcile").start()

    def _handle_composer_key(self, key: Any) -> None:
        if self.status_line.startswith("Loading "):
            self.status_line = ""
        self._interaction_serial += 1
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
        if name in ("enter", "tab") and self.composer.completion:
            selected = self.composer.completion[self.composer.completion_index]
            paths = [f"/{_slash_public_path(entry)}" for entry in self.catalog_cache if entry.slash_visible]
            paths.extend(
                f"/tui {control}" for control in ("help", "sessions", "session", "new", "views", "model", "thinking", "cancel", "quit", "exit")
            )
            is_group = selected not in paths
            if is_group or (name == "tab" and any(path.startswith(selected + " ") for path in paths)):
                self.composer.set_text(selected + " ")
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
        elif outcome.action == ComposerAction.SUBMIT and outcome.text is not None:
            line = outcome.text.strip()
            if not line:
                return
            if line.startswith("/"):
                self._dispatch_slash_command(line)
            else:
                self._dispatch_user_prompt(line)

    def _sync_slash_completion(self, *, dismissed: bool = False) -> None:
        """Refresh the slash-name popup after one composer key; preserve selection."""
        if dismissed:
            self.composer.set_completion(())
            return
        candidates = self._completion_candidates()
        if not candidates:
            self.composer.set_completion(())
            return
        previous = self.composer.completion
        index = self.composer.completion_index
        selected = previous[index] if previous and 0 <= index < len(previous) else None
        self.composer.set_completion(candidates, index=candidates.index(selected) if selected in candidates else 0)
        descriptions: dict[str, str] = {}
        for entry in self.catalog_cache:
            path = f"/{_slash_public_path(entry)}"
            properties = entry.argument_schema.get("properties", {})
            required = entry.argument_schema.get("required", ())
            properties = properties if isinstance(properties, Mapping) else {}
            required = required if isinstance(required, (tuple, list)) else ()
            hints = " ".join(f"<{name}>" if name in required else f"[{name}]" for name in properties)
            descriptions[path] = f"{hints} — {entry.description}" if hints else entry.description
        self.composer.completion_descriptions = descriptions
        self.composer.completion_groups = {
            candidate
            for candidate in candidates
            if any(f"/{_slash_public_path(entry)}".startswith(candidate + " ") for entry in self.catalog_cache if entry.slash_visible)
            or candidate == "/tui"
        }

    def _completion_candidates(self) -> list[str]:
        """Slash-command paths matching the draft prefix (catalog + /tui controls)."""
        draft = self.composer.value
        if not draft.startswith("/") or "\n" in draft or self.composer.cursor != len(draft):
            return []
        prefix = draft[1:].lstrip()
        names = [_slash_public_path(entry) for entry in self.catalog_cache if entry.slash_visible]
        names.extend(f"tui {control}" for control in ("help", "sessions", "session", "new", "views", "model", "thinking", "cancel", "quit", "exit"))
        parent, _, partial = prefix.rpartition(" ")
        base = f"{parent} " if parent else ""
        candidates = {base + name[len(base) :].split(" ", 1)[0] for name in names if name.startswith(base) and name[len(base) :].startswith(partial)}
        return sorted(f"/{name}" for name in candidates if name and name != parent)

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
            if not self.composer.value:
                self.composer.set_text(prompt)
            self.status_line = "Run in progress · Esc to interrupt"
            return
        self.status_line = ""
        self._run_active = True
        session_id, generation = self.session_id, self._generation

        def run_worker() -> None:
            try:
                res = self.client.run(session_id, RunSpec(prompt))
                status = res.status
                self.event_queue.put(
                    {
                        "kind": "run_status",
                        "status": status,
                        "error": res.error,
                        "run_id": res.run_id,
                        "session_id": session_id,
                        "generation": generation,
                    }
                )
                if status == "waiting" and res.pending_approval is not None:
                    try:
                        gate = _approval_gate_from_payload(res.pending_approval, run_id=res.run_id, revision_id=res.revision_id)
                    except ValueError as exc:
                        self._queue_notice(f"Approval receipt malformed: {exc}", tone="error", session_id=session_id, generation=generation)
                    else:
                        self.event_queue.put(
                            {
                                "kind": "run_waiting",
                                "gate": gate,
                                "session_id": session_id,
                                "generation": generation,
                            }
                        )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
                    self.event_queue.put({"kind": "run_rejected", "prompt": prompt, "session_id": session_id, "generation": generation})
                    self._upgrade_pending_notice("the prompt", session_id=session_id, generation=generation)
                else:
                    self.event_queue.put(
                        {
                            "kind": "run_status",
                            "status": "failed",
                            "error": str(exc),
                            "session_id": session_id,
                            "generation": generation,
                        }
                    )
            except Exception as exc:
                self.event_queue.put(
                    {
                        "kind": "run_status",
                        "status": "failed",
                        "error": str(exc),
                        "session_id": session_id,
                        "generation": generation,
                    }
                )

        threading.Thread(target=run_worker, daemon=True, name="CoreRun").start()

    def _dispatch_slash_command(self, line: str) -> None:
        rest = line[1:].strip()
        if not rest:
            return
        head, _, arg_str = rest.partition(" ")

        # "/tui <control>" always reaches the client's own controls, so a plugin
        # that declares e.g. "help" cannot make local navigation unreachable.
        if head == "tui":
            local, _, local_args = arg_str.strip().partition(" ")
            if not local:
                self._show_help()
                return
            if self._run_local_control(local, local_args.strip()):
                return
            self.terminal.commit_rows([self.styles.fg("error", f"Unknown TUI control: /tui {_safe(local)}")])
            return

        descriptors = self._catalog()
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
            return
        if ranked:
            self.terminal.commit_rows([f"{self.styles.fg('error', f'Unknown command: /{_safe(rest)}')} (try /help)"])
            return

        # A bare name is a local control only when no admitted command owns it.
        if head not in {c.name for c in descriptors} and self._run_local_control(head, arg_str):
            return
        self._dispatch_catalog_command(None, rest)

    def _run_local_control(self, cmd: str, arg_str: str) -> bool:
        """Run one client-local control; False when the name is not a control."""
        if cmd in ("help", "?"):
            self._show_help()
            return True
        if cmd == "sessions":
            self._show_sessions()
            return True
        if cmd == "session":
            if arg_str:
                self._switch_session(arg_str.strip())
            return True
        if cmd == "new":
            self._create_session()
            return True
        if cmd == "cancel":
            self._cancel_active_run()
            return True
        if cmd == "views":
            self._show_views(arg_str)
            return True
        if cmd == "model":
            self._tui_model(arg_str.strip())
            return True
        if cmd == "thinking":
            self._tui_thinking(arg_str.strip())
            return True
        if cmd in ("quit", "exit"):
            self.running = False
            return True
        return False

    def _action_schema(self, name: str) -> Mapping[str, Any] | None:
        """The target command's declared argument schema, the form's field authority."""
        for descriptor in self._catalog():
            if descriptor.name == name:
                return descriptor.argument_schema
        return None

    def _catalog(self) -> tuple[CommandDescriptor, ...]:
        if not self.catalog_cache:
            self._load_catalog()
        return self.catalog_cache

    def _switch_session(self, session_id: str) -> None:
        """Rebind the follower to another session.

        Each session keeps its own committed cursor, so A -> B -> A replays
        nothing that was already settled in A; in-flight results from the
        previous session are dropped by the generation/session guard.
        """
        self._generation += 1
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
        self._tool_headers.clear()
        self._clear_stream()
        # A new stream is about to start: its first frame reconciles.
        self._stream_ready = False
        self.status_line = ""
        self._maybe_welcome()
        self._start_event_stream()

    def _create_session(self) -> None:
        try:
            res = self.client.create_session()
            new_id = res.session_id
        except Exception as exc:
            self.terminal.commit_rows([self.styles.fg("error", f"Failed to create session: {_safe(exc)}")])
            return
        if new_id:
            self._switch_session(new_id)

    def _show_help(self) -> None:
        heading = self.styles.render("accent", "=== VoidCode Terminal Controls ===")
        lines = [
            heading,
            "  /tui help              Show this help (always available)",
            "  /tui sessions          List available sessions",
            "  /tui session <id>      Switch active session",
            "  /tui new               Create new session",
            "  /tui views [name]      Open plugin views/forms (for example goal)",
            "  /tui model [provider/model]  Show or switch the model (embedded only)",
            "  /tui thinking [level|off]  Show or set thinking level (embedded only)",
            "  /tui cancel            Cancel active session run",
            "  /tui quit              Exit TUI",
            "  Esc                    Cancel a run, or close/deny a dialog",
            "  Ctrl+C                 Clear input; press again within 1.5s to quit",
            "  Ctrl+D                 Quit when input is empty",
            "  (/views, /sessions, ... are local when no admitted command owns the name)",
            self.styles.render("accent", "=== Admitted Commands ==="),
        ]
        if not self.catalog_cache:
            self._load_catalog()
        for c in self.catalog_cache:
            if not c.slash_visible:
                continue
            lines.append(f"  /{_slash_public_path(c):<16} {c.description}")
        if self._shortcut_bindings or self._shortcut_conflicts:
            lines.append(self.styles.render("accent", "=== Shortcuts ==="))
            for chord in sorted(self._shortcut_bindings):
                descriptor = self._shortcut_bindings[chord]
                lines.append(f"  {chord:<18} {_safe(descriptor.description)}")
            for chord in sorted(self._shortcut_conflicts):
                claimants = ", ".join(f"/{_slash_display(entry)}" for entry in self._shortcut_conflicts[chord])
                lines.append(f"  {chord:<18} conflicts: {claimants} (unbound)")
        self.terminal.commit_rows(lines)

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

    def _tui_switch_model(self, field: str, value: str) -> None:
        """Rewrite the workspace model section and reload once; refusals stay visible."""
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
            try:
                normalized = normalize_reasoning_effort(value)
            except ValueError as exc:
                self.terminal.commit_rows([self.styles.fg("error", f"Invalid effort: {_safe(exc)}")])
                return
            if normalized is None:
                self.terminal.commit_rows([self.styles.fg("error", "Invalid effort: value is required")])
                return
            new_value = normalized
            levels = getattr(getattr(self.upgrade, "model", None), "reasoning_levels", None)
            if isinstance(levels, (list, tuple)) and levels and isinstance(new_value, str) and new_value != "off" and new_value not in levels:
                supported = _levels_text(getattr(self.upgrade, "model", None))
                self.terminal.commit_rows([self.styles.fg("error", f"Unsupported level for this model: {_safe(new_value)} ({_safe(supported)})")])
                return
        if self._workspace_dir is None:
            self.terminal.commit_rows([self.styles.fg("error", "No workspace: cannot rewrite configuration")])
            return
        from pathlib import Path

        from voidcode.capabilities.files._replace import replace_file
        from voidcode.config import CONFIG_FILE_NAME, parse_config

        path = Path(self._workspace_dir) / CONFIG_FILE_NAME
        try:
            expected = path.read_bytes() if path.is_file() else None
            payload: dict[str, object] = json.loads(expected.decode("utf-8")) if expected is not None else {"schema_version": 1}
            if not isinstance(payload, dict):
                raise ValueError("config must be an object")
            raw_section = payload.get("model", {})
            if not isinstance(raw_section, (dict, type(None))):
                raise ValueError("model section must be an object")
            section = dict(raw_section or {})
            section[field] = new_value
            payload["model"] = section
            parse_config(payload)  # validate before writing
            replace_file(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), expected_content=expected)
        except Exception as exc:
            self.terminal.commit_rows([self.styles.fg("error", f"Config rewrite refused: {_safe(exc)}")])
            return
        revision = self.catalog_revision or self._displayed_revision()
        try:
            result = self.client.invoke_command(self.session_id, "core.reload", {}, expected_revision=revision)
        except Exception as exc:
            self.terminal.commit_rows([self.styles.fg("error", f"Reload failed: {_safe(exc)}")])
            return
        if result.status == "ok":
            data = result.data if isinstance(result.data, Mapping) else {}
            to_revision = str(data.get("to_revision_id", "")) if data else ""
            self.terminal.commit_rows([self.styles.fg("success", f"[Model switched: revision {to_revision or revision} admitted]")])
        else:
            self.terminal.commit_rows([self.styles.fg("error", f"Reload {result.status}: {_safe(result.error)}")])
        self._refresh_health_async()

    def _displayed_revision(self) -> str:
        if not self.catalog_cache:
            self._load_catalog()
        return self.catalog_revision

    def _show_sessions(self) -> None:
        try:
            sess_list = self.client.list_sessions()
            if not sess_list:
                self.terminal.commit_rows([self.styles.fg("warning", "No active sessions.")])
                return
            options = tuple(
                ChoiceOption(
                    id=s.session_id,
                    label=f"{s.session_id[:8]} (turn {s.turn}, {s.status})" + (" [current]" if s.session_id == self.session_id else ""),
                )
                for s in sess_list
            )
            data = ChoicesViewData(options=options, action="tui.switch_session", argument_name="session_id")
            overlay = InteractiveChoices(data, revision_id=self._displayed_revision(), title="Active Sessions")
            for idx, s in enumerate(sess_list):
                if s.session_id == self.session_id:
                    overlay.selected_index = idx
                    break
            self.active_overlay = overlay
        except Exception as exc:
            self.terminal.commit_rows([self.styles.fg("error", f"Failed to list sessions: {_safe(exc)}")])

    def _show_views(self, name_filter: str = "") -> None:
        """Render plugin views only when explicitly requested.

        Passive views are committed inline. Interactive choices/forms/view-
        confirmations are snapshots, not pending durable requests, so this is
        the one path allowed to mount one; true approval and confirmation gates
        are restored separately from their durable events.
        """
        self._interaction_serial += 1
        try:
            snapshot = self.client.views(self.session_id)
        except Exception as exc:
            self.terminal.commit_rows([self.styles.fg("error", f"Failed to read views: {_safe(exc)}")])
            return
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
        if isinstance(data, MarkdownViewData) and data.actions:
            self.active_overlay = InteractiveMarkdown(data, revision_id=revision, title=view.title)
        elif view.view_type == "choices":
            from voidcode.sdk.views import ChoicesViewData

            assert isinstance(data, ChoicesViewData)
            self.active_overlay = InteractiveChoices(data, revision_id=revision, title=view.title)
        elif view.view_type == "form":
            from voidcode.sdk.views import FormViewData

            assert isinstance(data, FormViewData)
            form = InteractiveForm(data, action_schema=self._action_schema(data.action), revision_id=revision, title=view.title)
            self._maybe_load_form_data(form)
            self.active_overlay = form
        elif view.view_type == "confirmation":
            assert isinstance(data, ConfirmationViewData)
            self.active_overlay = InteractiveConfirmation(data, revision_id=revision)
        else:
            return False
        return True

    def _maybe_load_form_data(self, form: InteractiveForm) -> None:
        """Run the declared load_action off the UI thread, exactly when it is read-only."""
        if not form.load_action:
            return
        load_action = str(form.load_action)
        catalog = CommandCatalog(revision_id=self.catalog_revision or "rev-unknown", commands=self._catalog())
        if not is_read_only_load(form.view, catalog):
            form.load_error = f"load_action '{load_action}' must declare exactly the read effect"
            return

        session_id, generation = self.session_id, self._generation
        target = form

        def worker() -> None:
            payload: dict[str, Any] = {
                "kind": "form_load",
                "form": target,
                "session_id": session_id,
                "generation": generation,
            }
            try:
                res = self.client.invoke_command(
                    session_id,
                    load_action,
                    expected_revision=target.revision_id,
                )
                data = res.data
                if isinstance(data, Mapping):
                    payload["data"] = data
                elif res.error:
                    payload["error"] = str(res.error)
            except Exception as exc:
                payload["error"] = str(exc)
            self.event_queue.put(payload)

        threading.Thread(target=worker, daemon=True, name="CoreFormLoad").start()

    def _prepare_command_input_async(self, descriptor: CommandDescriptor, *, arguments: Mapping[str, Any] | None = None) -> None:
        """Read optional plugin input off the UI thread without invoking its action."""
        self._interaction_serial += 1
        serial = self._interaction_serial
        session_id, generation, revision = self.session_id, self._generation, self.catalog_revision
        owned_arguments = dict(arguments or {})
        # Hidden actions (Alt+A monitor) show their human description, never a
        # raw /dotted id a user could mistake for a slash command.
        self.status_line = f"Loading {_safe(descriptor.description) if not descriptor.slash_visible else '/' + _slash_public_path(descriptor)}..."

        def worker() -> None:
            frame: dict[str, Any] = {
                "kind": "command_input_ready",
                "serial": serial,
                "revision_id": revision,
                "session_id": session_id,
                "generation": generation,
                "action": descriptor.name,
                "arguments": owned_arguments,
            }
            try:
                snapshot = self.client.command_input(session_id, descriptor.name, revision, arguments=owned_arguments)
                if snapshot.revision_id != revision:
                    raise ValueError("command input revision changed; refresh the catalog")
                presentation = snapshot.presentation
                frame["view"] = PluginViewDescriptor(
                    owner=descriptor.owner, name=descriptor.name, title=presentation.title, view_type=presentation.view_type, data=presentation.data
                )
                frame["refresh_after_ms"] = presentation.refresh_after_ms
            except Exception as exc:
                frame["error"] = str(exc)
            self.event_queue.put(frame)

        threading.Thread(target=worker, daemon=True, name="CoreCommandInput").start()

    def _refresh_live_input_async(self) -> None:
        """Re-read the open live view's source once; stale replies never render."""
        live = self._live_input
        if live is None:
            return
        self._live_serial += 1
        serial = self._live_serial
        session_id, generation = self.session_id, self._generation
        action = str(live.get("name", ""))
        revision = str(live.get("revision", ""))
        owned_arguments = dict(live.get("arguments") or {})
        source = live.get("source")

        def worker() -> None:
            frame: dict[str, Any] = {
                "kind": "live_input_ready",
                "serial": serial,
                "revision_id": revision,
                "session_id": session_id,
                "generation": generation,
                "action": action,
                "arguments": owned_arguments,
            }
            try:
                snapshot = self.client.command_input(session_id, action, revision, arguments=owned_arguments)
                if snapshot.revision_id != revision:
                    raise ValueError("command input revision changed; refresh the catalog")
                presentation = snapshot.presentation
                frame["view"] = PluginViewDescriptor(
                    owner="live", name=action, title=presentation.title, view_type=presentation.view_type, data=presentation.data
                )
                frame["refresh_after_ms"] = presentation.refresh_after_ms
            except Exception as exc:
                frame["error"] = str(exc)
            if source is not None:
                frame["source"] = source
            self.event_queue.put(frame)

        threading.Thread(target=worker, daemon=True, name="CoreLiveInput").start()

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

        schema = cmd_desc.argument_schema if isinstance(cmd_desc.argument_schema, Mapping) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = schema.get("required") if isinstance(schema.get("required"), (list, tuple)) else ()
        assert isinstance(properties, Mapping) and isinstance(required, (list, tuple))

        arguments: dict[str, Any]
        if not arg_str and required and properties:
            from voidcode.sdk.views import FormField, FormFieldType, FormViewData

            field_types: dict[str, FormFieldType] = {"string": "text", "integer": "integer", "number": "number", "boolean": "boolean"}
            if all(isinstance(value, Mapping) and value.get("type") in field_types for value in properties.values()):
                fields = tuple(
                    FormField(
                        name=name,
                        label=str(spec.get("description") or name),
                        field_type=field_types[spec["type"]],
                        required=name in required,
                    )
                    for name, spec in properties.items()
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
            and isinstance(properties[required[0]], Mapping)
            and properties[required[0]].get("type") == "string"
        ):
            # A single required string schema maps slash trailing text generically.
            arguments = {required[0]: arg_str}
        else:
            try:
                parsed = json.loads(arg_str)
            except Exception:
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
        width = self.terminal.width
        if self._run_active or self._stream_dispatch is not None:
            self._spinner_tick += 1
        # Context row (server truth) plus a SEPARATE transient notice row.
        # footer=off hides the context row only: gates/errors always show.
        context = self._footer_context(width)
        context_row = (self.styles.fg("text", context) if context else "",)
        notice = self.styles.fg("dim", self.status_line) if self.status_line else ""
        quit_hint = self.styles.fg("dim", "Press Ctrl+C again to exit") if time.monotonic() - self._quit_armed_at < 1.5 else ""
        notice_rows = tuple(row for row in (notice, quit_hint) if row)
        footer_rows = tuple(row for row in (*context_row, *notice_rows) if row)
        if self.pending_approval is not None:
            rows = self.pending_approval.render_rows(width, self.styles)
            frame = TuiFrame(transcript_rows=(), chrome_rows=(*rows, *footer_rows), cursor=None)
            self.terminal.paint_frame(frame)
        elif self.active_overlay is not None:
            overlay_rows = (
                self.active_overlay.render_rows(width, self.styles, max_rows=max(6, getattr(self.terminal, "height", 24) - len(footer_rows)))
                if isinstance(self.active_overlay, (InteractiveMarkdown, InteractiveChoices))
                else self.active_overlay.render_rows(width, self.styles)
            )
            frame = TuiFrame(transcript_rows=(), chrome_rows=(*overlay_rows, *footer_rows), cursor=None)
            self.terminal.paint_frame(frame)
        else:
            self.composer.set_width(width)
            height = getattr(self.terminal, "height", 24)
            available = max(1, height - len(footer_rows))
            self.composer.set_max_height(self.composer.input_height(available, status_visible=self.border == "borderless" and self._run_active))
            composer_rows = self.composer.render(
                self._border_status() if self.border == "borderless" else "",
                border_status=self._border_status(),
            )
            cursor = self.composer.cursor_position
            live = self._live_stream_rows(width)
            popup = list(self.composer.completion_rows(max_rows=max(1, min(6, available - len(composer_rows)))))
            live_space = max(0, available - len(composer_rows) - len(popup))
            live = live[-live_space:] if live_space else []
            if cursor is not None and (live or popup):
                row, column = cursor
                cursor = (row + len(live) + len(popup), column)
            frame = TuiFrame(transcript_rows=(), chrome_rows=(*live, *popup, *composer_rows, *footer_rows), cursor=cursor)
            self.terminal.paint_frame(frame)


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
