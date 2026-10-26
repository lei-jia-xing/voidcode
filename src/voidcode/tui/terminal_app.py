"""Interactive inline TUI application for the VoidCode core transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import threading
import time
from collections.abc import Mapping
from typing import Any

from voidcode.core.events import EventType

from .client import CoreClientError, CoreHttpClient, StreamCancellation
from .composer import Composer, ComposerAction
from .keys import Key, KeyDecoder
from .region import TuiFrame
from .term import Terminal, escape_terminal_controls
from .theme import Theme, appearance_mode, palette_names, resolve_theme
from .views import (
    InteractiveApproval,
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    render_assistant_markdown,
    render_plugin_view,
    render_tool_call,
    render_tool_result,
)

logger = logging.getLogger(__name__)

#: Control bytes inside model/plugin text must never reach the terminal as
#: commands (they could clear scrollback or switch buffers), so they are made
#: visible before the row is styled or committed.

#: A lone ESC byte is ambiguous (it may start a sequence) until no further input
#: follows: the decoder buffers it, and the loop resolves it after this window.
#: Matches the 50 ms window the pre-cutover client used.
_ESCAPE_FLUSH_SECONDS = 0.05


def _safe(value: object) -> str:
    """One-line, control-free rendering of any plugin- or model-provided value."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return escape_terminal_controls(text, preserve_tabs=False)


def json_safe(value: object) -> object:
    """Round-trip unknown payload pieces through JSON so anything is serializable."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
    except TypeError, ValueError:
        return str(value)


def _prefixed_rows(prefix: str, text: str, theme: Theme, *, token: str) -> list[str]:
    """Prefix sanitized multiline text, one row per explicit line.

    Terminal owns physical width wrapping, so this helper never clips content.
    """
    label = f"{theme.style(token, bold=True).render(prefix, color_system=theme.color_system)} "
    clean = escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True)
    rows: list[str] = []
    for index, line in enumerate(clean.split("\n")):
        head = label if index == 0 else " " * len(prefix) + " "
        rows.append(f"{head}{line}")
    return rows or [label]


def _command_result_text(result: Mapping[str, Any]) -> str:
    """One command result's human text: error, then output, then pretty data.

    ``CommandResult`` carries both human text (``output``) and machine data
    (``data``). The TUI renders the human text when present and falls back to
    JSON for data-only receipts (for example the core ``show`` command). The
    fallback preserves an empty mapping and nested falsy values because it
    serializes the value itself, never a truthiness test.
    """
    status = str(result.get("status", ""))
    error = result.get("error")
    output = result.get("output")
    if status == "error" and isinstance(error, str) and error:
        return error
    if isinstance(output, str) and output:
        return output
    data = result.get("data")
    if data is not None:
        return json.dumps(json_safe(data), indent=2, ensure_ascii=False)
    return ""


def _command_result_rows(name: str, result: Mapping[str, Any], theme: Theme) -> list[str]:
    """One visible command-result block shared by direct receipts and events.

    A direct core read receipt and a durable plugin ``core.command_completed``
    payload are the same ``CommandResult`` shape. Rendering both here keeps the
    format, error precedence, and multi-line handling identical; no command
    name needs a branch. Unknown statuses never render as success.
    """
    label = _safe(name or "command")
    status = str(result.get("status", ""))
    text = _command_result_text(result)
    if status == "error":
        return _prefixed_rows(f"[error] Command /{label} failed:", text, theme, token="error")
    if status == "pending_confirmation":
        return [theme.fg("warning", f"[wait] Command /{label} awaits confirmation")]
    if status == "ok":
        return _prefixed_rows(f"[ok] Command /{label}:", text, theme, token="success")
    return _prefixed_rows(f"Command /{label} [{status or 'unknown'}]:", text, theme, token="warning")


def _notice_row(text: object, tone: str, theme: Theme) -> str:
    token = {"ok": "success", "warning": "warning", "error": "error"}.get(tone, "accent")
    return theme.fg(token, f"[{_safe(text)}]")


class CoreTuiApp:
    """HTTP-backed inline TUI using Terminal and TuiFrame."""

    def __init__(
        self,
        client: CoreHttpClient,
        *,
        session_id: str | None = None,
        terminal: Terminal | None = None,
    ) -> None:
        self.client = client
        self.terminal = terminal or Terminal.open()
        self.owns_terminal = terminal is None
        self.session_id = session_id or ""
        self.running = False
        mode = appearance_mode(self.terminal.query_default_background_rgb())
        self.theme = resolve_theme(mode=mode, color_system=self.terminal.color_system)
        self.composer = Composer(theme=self.theme, width=self.terminal.width, placeholder="Type a message or /command...")
        self.key_decoder = KeyDecoder()
        self._escape_flush_at = 0.0
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._session_cursors: dict[str, int] = {}
        self._generation = 0
        #: Last known server identity/liveness from /api/health. The fence bit
        #: pauses dispatch; the identity triple detects a replaced server.
        self.upgrade: dict[str, Any] | None = None
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
        self.active_overlay: InteractiveChoices | InteractiveConfirmation | InteractiveForm | None = None
        # Native tool approval (core permission gate) is not a plugin Action
        # confirmation: it resumes the parked run with its own request id.
        self.pending_approval: InteractiveApproval | None = None
        #: Confirmation requests that arrived while another overlay owned the live
        #: region, keyed by request_id in request order: none is ever dropped.
        self.queued_confirmations: dict[str, InteractiveConfirmation] = {}
        #: Request ids already terminally decided: a late duplicate fact or direct
        #: result for one of these can never re-open it. ponytail: retained for the
        #: process lifetime (one id per decision); evict if that ever matters.
        self.resolved_confirmations: set[str] = set()
        self.catalog_cache: list[dict[str, Any]] = []
        #: The capability revision the displayed catalog came from; slash dispatch
        #: echoes it verbatim. Rendered overlays carry their own captured revision.
        self.catalog_revision: str = ""
        self.status_line = "Connecting to server..."
        self._sse_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        #: Owns the current generation's SSE connection so quit can wake a blocked idle read
        #: before joining. A replacement stream closes this handle and installs its own; workers
        #: capture their handle locally so a late detach from an old generation cannot clear it.
        self._stream_cancel: StreamCancellation | None = None

    def run(self) -> int:
        self.running = True
        try:
            self._init_session()
            self._load_catalog()
            self._start_sse_stream()
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
                if self._sse_thread and self._sse_thread.is_alive():
                    self._sse_thread.join(timeout=1.0)
                if self.owns_terminal:
                    self.terminal.close()

    def _init_session(self) -> None:
        if self.session_id:
            # Explicit requested session: must exist on the backend; never silently create new
            record = self.client.get_session(self.session_id)
            actual_id = str(record.get("session_id") or "")
            if not actual_id:
                raise RuntimeError(f"Server returned invalid session record for {self.session_id!r}")
            self.session_id = actual_id
        else:
            # No session requested: list sessions; if none exist, create one
            sessions = self.client.list_sessions(limit=1)
            if sessions:
                actual_id = str(sessions[0].get("session_id") or "")
                if not actual_id:
                    raise RuntimeError("Server returned session without session_id")
                self.session_id = actual_id
            else:
                new_session = self.client.create_session()
                actual_id = str(new_session.get("session_id") or "")
                if not actual_id:
                    raise RuntimeError("Server created session without session_id")
                self.session_id = actual_id
        # Routine attachment is quiet: ids are diagnostics, not idle chrome.
        self.status_line = ""

    def _load_catalog(self) -> None:
        try:
            revision, commands = self.client.command_catalog()
            self.catalog_cache = commands
            self.catalog_revision = revision
        except Exception as exc:
            self.catalog_cache = []
            self.catalog_revision = ""
            self.status_line = f"Catalog load failed: {_safe(exc)}"
            raise RuntimeError(f"Failed to fetch command catalog from server: {exc}") from exc

    def _start_sse_stream(self) -> None:
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
                            tagged = dict(frame)
                            tagged["generation"] = generation
                            tagged["session_id"] = session_id
                            self.event_queue.put(tagged)
                            if frame.get("kind") == "server_restart":
                                break
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    if self._stop_event.is_set() or generation != self._generation:
                        return
                    logger.debug("SSE stream reconnecting after: %s", exc)
                    await asyncio.sleep(1.0)

        def run_worker() -> None:
            asyncio.run(stream_worker())

        self._sse_thread = threading.Thread(target=run_worker, daemon=True, name=f"CoreTuiSSE{generation}")
        self._sse_thread.start()

    def _main_loop(self) -> int:
        while self.running:
            self._drain_events()
            self._process_input()
            self._render_frame()
            time.sleep(0.033)  # ~30 fps redraw loop
        return 0

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
            if not self._stream_ready and ("events" in frame or kind in ("event", "server_restart", "error")):
                # A frame on a not-ready stream means a (re)connection was just
                # established: run the one reconciliation path. The worker
                # rearms the flag on every reconnect, so announced restarts and
                # silent drops share this code path — no per-frame re-trigger.
                self._stream_ready = True
                self._reconcile_connection_async()
            if "events" in frame and isinstance(frame["events"], list):
                for ev in frame["events"]:
                    self._handle_single_event(ev, rows_to_commit)
            elif kind == "event" and "event" in frame:
                self._handle_single_event(frame["event"], rows_to_commit)
            elif kind == "notice":
                rows_to_commit.append(_notice_row(frame.get("text"), str(frame.get("tone", "info")), self.theme))
            elif kind == "command_done":
                raw_name = str(frame.get("name") or "command")
                status = str(frame.get("status", ""))
                # A real pending gate and a fenced core own the status line.
                # Never replace "approval required"/"confirmation required"/
                # "upgrade in progress" with a late completion notice.
                fenced = isinstance(self.upgrade, dict) and self.upgrade.get("fenced") is True
                gate_open = self.pending_approval is not None or isinstance(self.active_overlay, InteractiveConfirmation)
                if status in ("ok", "error") and not fenced and not gate_open:
                    self.status_line = f"/{_safe(raw_name)}: {status}"
                receipt = frame.get("result")
                if isinstance(receipt, Mapping):
                    rows_to_commit.extend(_command_result_rows(raw_name, receipt, self.theme))
            elif kind == "approval":
                approval = frame.get("approval")
                if isinstance(approval, dict):
                    self.pending_approval = InteractiveApproval(approval)
                    self.status_line = f"Tool approval required: {self.pending_approval.tool_name}"
            elif kind == "approval_accepted":
                # The server accepted this exact request: close the gate it
                # matches, or nothing when a newer request already replaced it.
                accepted = str(frame.get("request_id", ""))
                self._in_flight.discard(accepted)
                current = self.pending_approval
                if accepted and current is not None and current.request_id == accepted:
                    self.pending_approval = None
                    self._present_queued_confirmation()
                decision = str(frame.get("decision", ""))
                status = str(frame.get("status", ""))
                self.status_line = f"Approval {decision}: {status}"
            elif kind == "decision_done":
                # A failed submission releases its in-flight marker so the user
                # can retry; the gate/overlay itself stays mounted.
                self._in_flight.discard(str(frame.get("request_id", "")))
            elif kind == "run_status":
                status = str(frame.get("status", ""))
                if status == "waiting":
                    self.status_line = "Waiting for tool approval"
                if status in ("failed", "interrupted"):
                    detail = frame.get("error")
                    suffix = f": {_safe(detail)}" if detail else ""
                    rows_to_commit.append(self.theme.fg("warning", f"[Run {status}]{suffix}"))
                self.status_line = f"Run {status}" if status else self.status_line
            elif kind == "confirmation":
                confirmation = frame.get("confirmation")
                if isinstance(confirmation, dict):
                    ui_type = str(confirmation.get("ui_type", ""))
                    ui_data = confirmation.get("ui_data")
                    if ui_type == "confirmation" and isinstance(ui_data, dict):
                        # Bound to the revision the client invoked under, never to
                        # whatever happens to be current when the user decides.
                        self._show_or_queue_confirmation(InteractiveConfirmation(ui_data, revision_id=str(frame.get("revision_id", ""))))
            elif kind == "confirmation_accepted":
                # The server accepted this exact request: close the overlay it
                # matches, or nothing when a newer request already replaced it.
                # A decided id never resurrects: the durable decided fact (or
                # resolved_confirmations) already closed it permanently.
                accepted = str(frame.get("request_id", ""))
                self._in_flight.discard(accepted)
                overlay = self.active_overlay
                if (
                    accepted
                    and accepted not in self.resolved_confirmations
                    and isinstance(overlay, InteractiveConfirmation)
                    and overlay.request_id == accepted
                ):
                    self.active_overlay = None
                    self._present_queued_confirmation()
                decision = str(frame.get("decision", ""))
                status = str(frame.get("status", ""))
                self.status_line = f"Confirmation {decision}: {status}"
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
                    and isinstance(overlay, (InteractiveChoices, InteractiveForm))
                    and submit_key == f"invoke:{action}@{revision}"
                    and overlay.revision_id == revision
                ):
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
                if isinstance(snapshot, dict) and isinstance(serial, int) and serial == self._reconcile_serial and serial > self._reconcile_applied:
                    self._reconcile_applied = serial
                    self.upgrade = {
                        "fenced": snapshot.get("fenced") is True,
                        "stage": str(snapshot.get("stage") or ""),
                        "core_version": str(snapshot.get("core_version") or ""),
                        "image_digest": str(snapshot.get("image_digest") or ""),
                        "generation": snapshot.get("generation", -1),
                        "active_upgrade": snapshot.get("active_upgrade"),
                    }
                    if self.upgrade["fenced"]:
                        self.status_line = "Core upgrade in progress: dispatch paused"
                    else:
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
                    if isinstance(catalog, tuple) and len(catalog) == 2:
                        self.catalog_revision, self.catalog_cache = str(catalog[0]), list(catalog[1])
                    status = frame.get("session_status")
                    if isinstance(status, str) and status:
                        self.status_line = f"Run {status}" if status in ("running", "waiting") else self.status_line
                    if isinstance(failures, list) and failures:
                        rows_to_commit.append(self.theme.fg("warning", f"[Reconnect refresh incomplete: {', '.join(str(f) for f in failures)}]"))
            elif kind == "server_restart":
                # Watermark only (contract §8.2/§6): the worker already broke
                # its iterator and is reconnecting from our applied cursor.
                # Never set _session_cursors from the frame: queued but
                # uncommitted events would be dropped. Reconciliation runs on
                # the reconnected stream (first-frame trigger above), so an
                # announced restart and a silent drop share one path — and no
                # blocking request ever runs on this UI thread.
                self.status_line = "Core restarting -- reconnecting..."
            elif kind == "error":
                rows_to_commit.append(f"{self.theme.fg('error', '[Core stream error]')} {_safe(frame.get('error'))}")

        if rows_to_commit:
            self.terminal.commit_rows(rows_to_commit)

    def _handle_single_event(self, event: dict[str, Any], rows: list[str]) -> None:
        seq = int(event.get("sequence", 0))
        committed = self._session_cursors.get(self.session_id, 0)
        if seq <= committed:
            return  # Settled ledger: never replay already committed events
        self._session_cursors[self.session_id] = seq

        etype = str(event.get("type", ""))
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if etype == EventType.USER_MESSAGE:
            # The durable event is the single source of the prompt row: the
            # composer never commits it optimistically, and duplicates are
            # excluded by the per-session cursor above (not by a first-only flag).
            user = self.theme.style("accent", bold=True).render("User>", color_system=self.theme.color_system)
            rows.append(f"{user} {_safe(payload.get('content'))}")
        elif etype == EventType.MODEL_TURN:
            raw_turn = payload.get("result")
            turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
            output = turn.get("output")
            if isinstance(output, str) and output:
                rows.extend(render_assistant_markdown(output, self.terminal.width, self.theme))
            reasoning = turn.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                rows.extend(_prefixed_rows("Reasoning>", reasoning, self.theme, token="thinkingText"))
            tool_calls = turn.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    name = _safe(call.get("tool_name") or "tool")
                    rows.extend(render_tool_call(name, json_safe(call.get("arguments")), self.terminal.width, self.theme))
        elif etype in (EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED):
            raw_call = payload.get("call")
            call: dict[str, Any] = raw_call if isinstance(raw_call, dict) else {}
            raw_result = payload.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
            name = _safe(result.get("tool_name") or "unknown")
            rows.extend(render_tool_result(name, result, self.terminal.width, self.theme, interrupted=etype == EventType.TOOL_INTERRUPTED))
        elif etype == EventType.COMMAND_COMPLETED:
            raw_command_result = payload.get("result")
            command_result: dict[str, Any] = raw_command_result if isinstance(raw_command_result, dict) else {}
            raw_name = str(payload.get("name") or "command")
            rows.extend(_command_result_rows(raw_name, command_result, self.theme))
        elif etype == EventType.ACTIVATION_COMMITTED:
            # The displayed generation changed: the command catalog is
            # refetched and anything captured under the previous revision is
            # dropped — never relabelled, never retried. Optional views are
            # explicit-only, so activation never fetches or renders them.
            to_revision_id = str(payload.get("to_revision_id") or "")
            overlay = self.active_overlay
            dropped = overlay is not None and getattr(overlay, "revision_id", "") != to_revision_id
            if dropped:
                self.active_overlay = None
            # A queued request from another generation can never be resolved
            # against this one either, so it is discarded with the same rule.
            for queued_id in [i for i, q in self.queued_confirmations.items() if q.revision_id != to_revision_id]:
                del self.queued_confirmations[queued_id]
            self.pending_approval = None
            self._load_catalog()
            self._present_queued_confirmation()
            suffix = " (stale action discarded)" if dropped else ""
            rows.append(self.theme.fg("warning", f"[Capabilities changed: command catalog refreshed{suffix}]"))
        elif etype == EventType.CONFIRMATION_REQUESTED:
            # Durable pending fact: restores the modal after a reload, bound to
            # the revision that created the request (never the current one).
            request_id = _safe(payload.get("request_id"))
            ui_data = payload.get("ui_data")
            if request_id and str(payload.get("ui_type", "")) == "confirmation" and isinstance(ui_data, dict):
                self._show_or_queue_confirmation(InteractiveConfirmation(ui_data, revision_id=str(event.get("revision_id", ""))))
        elif etype == EventType.CONFIRMATION_DECIDED:
            # Durable reduction: the request is closed whether or not its modal
            # is on screen, so replayed history never resurrects it.
            decided = _safe(payload.get("request_id"))
            if decided:
                self.resolved_confirmations.add(decided)
                self.queued_confirmations.pop(decided, None)
                overlay = self.active_overlay
                if isinstance(overlay, InteractiveConfirmation) and overlay.request_id == decided:
                    self.active_overlay = None
                    self.status_line = "Confirmation resolved"
                    self._present_queued_confirmation()
        elif etype == EventType.APPROVAL_REQUESTED:
            raw_call = payload.get("call")
            call: dict[str, Any] = raw_call if isinstance(raw_call, dict) else {}
            request_id = _safe(payload.get("request_id"))
            if request_id:
                self.pending_approval = InteractiveApproval(
                    {"request_id": request_id, "tool_name": _safe(call.get("tool_name") or "tool"), "arguments": call.get("arguments")}
                )
                self.status_line = f"Tool approval required: {self.pending_approval.tool_name}"
        elif etype == EventType.APPROVAL_DECIDED:
            decision = _safe(payload.get("decision"))
            rows.append(self.theme.fg("warning", f"[Approval {decision}]"))
            # Durable reduction: the decided request closes whether the gate is
            # local-pending or was decided by a remote client — but only the
            # MATCHING id, so a newer parked request is never dropped.
            decided_approval = _safe(payload.get("request_id"))
            current = self.pending_approval
            if decided_approval and current is not None and current.request_id == decided_approval:
                self.pending_approval = None
                self._present_queued_confirmation()
        elif etype == EventType.RUN_INTERRUPTED:
            rows.append(self.theme.fg("warning", f"[Run interrupted: {_safe(payload.get('stop_cause'))}]"))
            # An authentic terminal fact means nothing is parked any more; a
            # stale gate must never outlive the run that asked for it.
            self.pending_approval = None
            self._present_queued_confirmation()
        elif etype == EventType.RUN_FAILED:
            rows.append(self.theme.fg("error", "[Run failed]"))
            self.pending_approval = None
            self._present_queued_confirmation()
        elif etype == EventType.OBSERVER_ERROR:
            rows.append(f"{self.theme.fg('error', '[Plugin observer error]')} {_safe(payload.get('error'))}")

    def _show_or_queue_confirmation(self, overlay: InteractiveConfirmation) -> None:
        """Show one pending confirmation, or retain it until the live region frees.

        The stored request id is the identity: the same request arriving again as a
        duplicate fact or as its live pending result never resets the modal, and a
        decided request never comes back.
        """
        request_id = overlay.request_id
        if not request_id or request_id in self.resolved_confirmations:
            return
        current = self.active_overlay
        if isinstance(current, InteractiveConfirmation) and current.request_id == request_id:
            return
        if self.active_overlay is None and self.pending_approval is None:
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
            if request_id in self.resolved_confirmations:
                continue
            self.active_overlay = overlay
            self.status_line = "Confirmation required"
            return

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
                if isinstance(self.active_overlay, InteractiveConfirmation):
                    # Never claim the request was cancelled: deny it for real.
                    self._resolve_confirmation("deny")
                elif self.active_overlay is not None:
                    self.active_overlay = None
                    self.status_line = "Overlay closed"
                    self._present_queued_confirmation()
                else:
                    self._cancel_active_run()
                continue
            if key.name == "ctrl+d":
                self.running = False
                return

            if self.pending_approval is not None:
                self._handle_approval_key(key)
            elif self.active_overlay is not None:
                self._handle_overlay_key(key)
            else:
                self._handle_composer_key(key)

    def _cancel_active_run(self) -> None:
        self.status_line = "Requesting cancellation..."
        session_id, generation = self.session_id, self._generation

        def worker() -> None:
            try:
                res = self.client.cancel(session_id)
                requested = bool(res.get("cancellation_requested"))
                self._queue_notice(
                    "Cancellation requested" if requested else "No active run to cancel",
                    tone="warning" if requested else "info",
                    session_id=session_id,
                    generation=generation,
                )
            except Exception as exc:
                self._queue_notice(f"Cancel error: {exc}", tone="error", session_id=session_id, generation=generation)

        threading.Thread(target=worker, daemon=True, name="CoreCancel").start()

    def _handle_approval_key(self, key: Any) -> None:
        """Native approval gate: scroll the preview, a=allow, d/Esc=deny; both hit /resume."""
        assert self.pending_approval is not None
        outcome = self.pending_approval.handle_key(key)
        if outcome is None:
            return
        self._resolve_approval(str(outcome.get("decision", "deny")))

    def _resolve_approval(self, decision: str) -> None:
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
                res = self.client.resume(session_id, approval_request_id=request_id, decision=decision)
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
            status = str(res.get("status", ""))
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
            self.active_overlay = None
            self.status_line = "Overlay closed"
            self._present_queued_confirmation()
            return

        if isinstance(self.active_overlay, InteractiveConfirmation):
            self._resolve_confirmation(str(outcome.get("decision", "deny")))
        elif isinstance(self.active_overlay, (InteractiveChoices, InteractiveForm)):
            action = str(outcome.get("action", ""))
            arguments = outcome.get("arguments", {})
            revision = self.active_overlay.revision_id
            # The overlay (with its typed inputs) stays mounted until the
            # server accepts: a late 503 must not lose the user's draft. The
            # in-flight marker stops a held Enter from spawning a second call.
            submit_key = f"invoke:{action}@{revision}"
            if submit_key in self._in_flight:
                return
            self._in_flight.add(submit_key)
            self.status_line = f"Dispatching /{action}..."
            self._invoke_async(action, arguments if isinstance(arguments, dict) else {}, revision, submit_key=submit_key)

    def _resolve_confirmation(self, decision: str) -> None:
        """Submit the stored request id's decision; the overlay never lies about it."""
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
            status = str(res.get("status", ""))
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
    ) -> None:
        """Run one command off the UI thread under the revision the user saw.

        Only a core read-only receipt is surfaced directly. Plugin commands
        project their result through the durable ``core.command_completed``
        event, and run/resume project through run events; rendering those direct
        answers too would duplicate settled rows. ``surface_result`` comes from
        catalog metadata (owner plus effects), never a client command list.
        """
        session_id, generation = self.session_id, self._generation

        def finish(status: str, result: Mapping[str, Any] | None = None) -> None:
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
                    self._upgrade_pending_notice(f"/{name}", session_id=session_id, generation=generation)
                else:
                    self._queue_notice(f"/{name} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                    finish("error")
                if submit_key is not None:
                    self.event_queue.put({"kind": "decision_done", "request_id": submit_key, "session_id": session_id, "generation": generation})
                return
            except Exception as exc:
                self._queue_notice(f"/{name} failed: {exc}", tone="error", session_id=session_id, generation=generation)
                finish("error")
                if submit_key is not None:
                    self.event_queue.put({"kind": "decision_done", "request_id": submit_key, "session_id": session_id, "generation": generation})
                return
            status = str(res.get("status", ""))
            if status == "pending_confirmation":
                confirmation = res.get("confirmation")
                if isinstance(confirmation, dict):
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
                    f"/{name} failed: {res.get('error') or 'error'}",
                    tone="error",
                    session_id=session_id,
                    generation=generation,
                )
            # An error notice already owns the failure text; a plugin/run result
            # is already projected by its durable event. Only the core read
            # receipt has no other display path.
            receipt = res if surface_result and status == "ok" else None
            finish(status, receipt)
            # Accepted (any non-exception outcome): the submitting overlay may
            # close now, but only if it is still the one that submitted.
            if submit_key is not None:
                self.event_queue.put(
                    {
                        "kind": "invoke_accepted",
                        "request_id": submit_key,
                        "action": name,
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
                session_status = str(self.client.get_session(session_id).get("status", ""))
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
        outcome = self.composer.handle_key(key)
        if outcome.action == ComposerAction.SUBMIT and outcome.text is not None:
            line = outcome.text.strip()
            if not line:
                return
            if line.startswith("/"):
                self._dispatch_slash_command(line)
            else:
                self._dispatch_user_prompt(line)

    def _dispatch_user_prompt(self, prompt: str) -> None:
        # No optimistic commit: the durable core.user_message event is the one
        # and only source of the prompt row on screen.
        if isinstance(self.upgrade, dict) and self.upgrade.get("fenced") is True:
            # Never queue a prompt behind a fence: the user re-sends after
            # reconnect. Composer._submit already cleared the draft, so restore
            # the exact text — but never overwrite a newer draft the user typed
            # while this key was being handled.
            if not self.composer.value.strip():
                self.composer.set_text(prompt)
            self.status_line = "Core is draining for an upgrade; retry after it restarts"
            return
        self.status_line = "Dispatching agent run..."
        session_id, generation = self.session_id, self._generation

        def run_worker() -> None:
            try:
                res = self.client.run(session_id, prompt)
                status = str(res.get("status", ""))
                self.event_queue.put(
                    {
                        "kind": "run_status",
                        "status": status,
                        "error": res.get("error"),
                        "session_id": session_id,
                        "generation": generation,
                    }
                )
                if status == "waiting":
                    record = self.client.get_session(session_id)
                    self.event_queue.put(
                        {
                            "kind": "waiting",
                            "session_id": session_id,
                            "generation": generation,
                            "session": record,
                        }
                    )
            except CoreClientError as exc:
                if exc.code == "upgrade_pending":
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
        parts = line[1:].strip().split(maxsplit=1)
        if not parts:
            return
        cmd = parts[0]
        arg_str = parts[1] if len(parts) > 1 else ""

        admitted = {str(c.get("name", "")) for c in self._catalog()}

        # "/tui <control>" always reaches the client's own controls, so a plugin
        # that declares e.g. "help" cannot make local navigation unreachable.
        if cmd == "tui":
            local, _, local_args = arg_str.partition(" ")
            if not local:
                self._show_help()
                return
            if self._run_local_control(local, local_args.strip()):
                return
            self.terminal.commit_rows([self.theme.fg("error", f"Unknown TUI control: /tui {_safe(local)}")])
            return

        # A bare name is a local control only when no admitted command owns it.
        if cmd not in admitted and self._run_local_control(cmd, arg_str):
            return

        self._dispatch_catalog_command(cmd, arg_str)

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
        if cmd == "theme":
            self._switch_theme(arg_str.strip())
            return True
        if cmd == "cancel":
            self._cancel_active_run()
            return True
        if cmd == "views":
            self._show_views(arg_str)
            return True
        if cmd in ("quit", "exit"):
            self.running = False
            return True
        return False

    def _switch_theme(self, name: str) -> None:
        """Switch the app palette live; bare ``/tui theme`` lists what ``mode`` admits."""
        if not name:
            options = [*palette_names("dark"), *palette_names("light")]
            current = f" (current: {self.theme.name})" if self.theme.name in options else ""
            self.terminal.commit_rows([f"Available themes: {', '.join(options)}{current}"])
            return
        try:
            self.theme = resolve_theme(name, color_system=self.terminal.color_system)
        except ValueError:
            options = [*palette_names("dark"), *palette_names("light")]
            self.terminal.commit_rows([self.theme.fg("error", f"Unknown theme: {_safe(name)} (choose: {', '.join(options)})")])
            return
        self.composer.set_appearance(theme=self.theme, style="band", gutter="prompt", padding="normal")
        self.status_line = f"Theme: {self.theme.name}"

    def _action_schema(self, name: str) -> Mapping[str, Any] | None:
        """The target command's declared argument schema, the form's field authority."""
        for descriptor in self._catalog():
            if descriptor.get("name") == name:
                schema = descriptor.get("argument_schema")
                return schema if isinstance(schema, Mapping) else None
        return None

    def _catalog(self) -> list[dict[str, Any]]:
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
        self.active_overlay = None
        self.queued_confirmations.clear()
        self.resolved_confirmations.clear()
        # A new stream is about to start: its first frame reconciles.
        self._stream_ready = False
        self.status_line = ""
        self._start_sse_stream()

    def _create_session(self) -> None:
        try:
            res = self.client.create_session()
            new_id = str(res.get("session_id", ""))
        except Exception as exc:
            self.terminal.commit_rows([self.theme.fg("error", f"Failed to create session: {_safe(exc)}")])
            return
        if new_id:
            self._switch_session(new_id)

    def _show_help(self) -> None:
        heading = self.theme.style("accent", bold=True).render("=== VoidCode Terminal Controls ===", color_system=self.theme.color_system)
        lines = [
            heading,
            "  /tui help              Show this help (always available)",
            "  /tui sessions          List available sessions",
            "  /tui session <id>      Switch active session",
            "  /tui new               Create new session",
            "  /tui theme [name]      Show or switch the color theme",
            "  /tui views [name]      Open plugin views/forms (for example goal)",
            "  /tui cancel            Cancel active session run",
            "  /tui quit              Exit TUI",
            "  Ctrl+C                 Cancel a run, or close/deny a dialog",
            "  Ctrl+D                 Quit",
            "  (/views, /sessions, ... are local when no admitted command owns the name)",
            self.theme.style("accent", bold=True).render("=== Admitted Commands ===", color_system=self.theme.color_system),
        ]
        if not self.catalog_cache:
            self._load_catalog()
        for c in self.catalog_cache:
            name = c.get("name", "")
            desc = c.get("description", "")
            lines.append(f"  /{name:<16} {desc}")
        self.terminal.commit_rows(lines)

    def _show_sessions(self) -> None:
        try:
            sess_list = self.client.list_sessions()
            lines = [self.theme.style("accent", bold=True).render("=== Active Sessions ===", color_system=self.theme.color_system)]
            for s in sess_list:
                sid = s.get("session_id", "")
                status = s.get("status", "idle")
                turn = s.get("turn", 0)
                marker = " >" if sid == self.session_id else "  "
                lines.append(f"{marker} {sid} (status={status}, turn={turn})")
            self.terminal.commit_rows(lines)
        except Exception as exc:
            self.terminal.commit_rows([self.theme.fg("error", f"Failed to list sessions: {_safe(exc)}")])

    def _show_views(self, name_filter: str = "") -> None:
        """Render plugin views only when explicitly requested.

        Passive views are committed inline. Interactive choices/forms/view-
        confirmations are snapshots, not pending durable requests, so this is
        the one path allowed to mount one; true approval and confirmation gates
        are restored separately from their durable events.
        """
        try:
            views_revision, v_list = self.client.views(self.session_id)
        except Exception as exc:
            self.terminal.commit_rows([self.theme.fg("error", f"Failed to read views: {_safe(exc)}")])
            return
        if name_filter:
            wanted = name_filter.strip().lower()
            v_list = [v for v in v_list if wanted in str(v.get("name", "")).lower() or wanted in str(v.get("title", "")).lower()]
        if not v_list:
            self.terminal.commit_rows([self.theme.fg("warning", "No plugin views available for this session.")])
            return
        for v in v_list:
            vtype = v.get("view_type")
            if vtype in ("choices", "form", "confirmation"):
                # The overlay carries the revision of this very envelope, so
                # the action it dispatches can never be relabelled later.
                if vtype == "choices":
                    self.active_overlay = InteractiveChoices(v.get("data", {}), revision_id=views_revision)
                elif vtype == "form":
                    form_data = v.get("data", {})
                    form = InteractiveForm(
                        form_data,
                        action_schema=self._action_schema(str(form_data.get("action", ""))),
                        revision_id=views_revision,
                    )
                    self._maybe_load_form_data(form)
                    self.active_overlay = form
                elif vtype == "confirmation":
                    self.active_overlay = InteractiveConfirmation(v.get("data", {}), revision_id=views_revision)
                return
            # Non-interactive view: render rows and commit
            rows = render_plugin_view(v, self.terminal.width, self.theme)
            self.terminal.commit_rows(rows)

    def _maybe_load_form_data(self, form: InteractiveForm) -> None:
        """Run the declared load_action off the UI thread, exactly when it is read-only."""
        if not form.load_action:
            return
        load_action = str(form.load_action)
        effects = [str(e).lower() for cmd in self._catalog() if cmd.get("name") == load_action for e in (cmd.get("effects") or [])]
        if effects != ["read"]:
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
                data = res.get("data")
                if isinstance(data, dict):
                    payload["data"] = data
                elif res.get("error"):
                    payload["error"] = str(res["error"])
            except Exception as exc:
                payload["error"] = str(exc)
            self.event_queue.put(payload)

        threading.Thread(target=worker, daemon=True, name="CoreFormLoad").start()

    def _dispatch_catalog_command(self, cmd_name: str, arg_str: str) -> None:
        cmd_desc = next((c for c in self._catalog() if c.get("name") == cmd_name), None)
        if cmd_desc is None:
            self.terminal.commit_rows([f"{self.theme.fg('error', f'Unknown command: /{_safe(cmd_name)}')} (try /help)"])
            return
        # The revision of the catalog this descriptor was read from.
        revision = self.catalog_revision
        # Core read commands answer over HTTP only; plugin commands and run/resume
        # already project through durable events. Classify by contract metadata,
        # never by a client-side command name list.
        effects = frozenset(str(effect) for effect in (cmd_desc.get("effects") or ()))
        surface_result = str(cmd_desc.get("owner", "")) == "core" and effects <= {"read"}

        schema = cmd_desc.get("argument_schema") or {}
        properties = schema.get("properties") or {}
        required = schema.get("required") or []

        arguments: dict[str, Any]
        if not arg_str:
            arguments = {}
        elif (
            len(properties) == 1
            and len(required) == 1
            and required[0] in properties
            and isinstance(properties[required[0]], dict)
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
                    self.terminal.commit_rows([self.theme.fg("error", f"Arguments must be valid JSON: {_safe(arg_str)}")])
                    return
            else:
                if not isinstance(parsed, dict):
                    self.terminal.commit_rows([self.theme.fg("error", f"Arguments must be a JSON object: {_safe(arg_str)}")])
                    return
                arguments = parsed

        self.status_line = f"Dispatching /{cmd_name}..."
        self._invoke_async(cmd_name, arguments, revision, surface_result=surface_result)

    def _render_frame(self) -> None:
        width = self.terminal.width
        # One status row for every branch, styled only when it has real text:
        # an empty reverse-video row would paint a blank band, not a quiet idle.
        status_text = (
            self.theme.style("text", reverse=True).render(self.status_line, color_system=self.theme.color_system) if self.status_line else ""
        )
        status_rows = (status_text,) if status_text else ()
        if self.pending_approval is not None:
            rows = self.pending_approval.render_rows(width, self.theme)
            frame = TuiFrame(transcript_rows=(), chrome_rows=(*rows, *status_rows), cursor=None)
            self.terminal.paint_frame(frame)
        elif self.active_overlay is not None:
            overlay_rows = self.active_overlay.render_rows(width, self.theme)
            frame = TuiFrame(transcript_rows=(), chrome_rows=(*overlay_rows, *status_rows), cursor=None)
            self.terminal.paint_frame(frame)
        else:
            self.composer.set_width(width)
            composer_rows = self.composer.render(status=status_text)
            cursor = self.composer.cursor_position
            frame = TuiFrame(transcript_rows=(), chrome_rows=tuple(composer_rows), cursor=cursor)
            self.terminal.paint_frame(frame)


def run_tui(server_url: str, *, token: str | None = None, session_id: str | None = None) -> int:
    """Launch the HTTP-backed generic TUI connected to a running core."""
    client = CoreHttpClient(server_url, token=token)
    app = CoreTuiApp(client, session_id=session_id)
    return app.run()
