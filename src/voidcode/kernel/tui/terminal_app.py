"""Interactive inline TUI application for the VoidCode kernel transport."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Mapping
from typing import Any

from .client import KernelHttpClient
from .composer import Composer, ComposerAction
from .keys import Key, KeyDecoder
from .region import TuiFrame
from .term import Terminal, escape_terminal_controls, visible_width
from .views import (
    InteractiveChoices,
    InteractiveConfirmation,
    InteractiveForm,
    render_plugin_view,
)

logger = logging.getLogger(__name__)

#: Control bytes inside model/plugin text must never reach the terminal as
#: commands (they could clear scrollback or switch buffers), so they are made
#: visible before the row is styled or committed.
_WRAP_WIDTH = 100

#: A lone ESC byte is ambiguous (it may start a sequence) until no further input
#: follows: the decoder buffers it, and the loop resolves it after this window.
#: The legacy app resolves it after the same 50 ms.
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


def _prefixed_rows(prefix: str, text: str, *, color: str) -> list[str]:
    """Wrap sanitized multi-line text under a colored prefix, one row per line."""
    label = f"\x1b[1;{color}m{prefix}\x1b[0m "
    clean = escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True)
    rows: list[str] = []
    for index, line in enumerate(clean.split("\n")):
        head = label if index == 0 else " " * len(prefix) + " "
        rows.append(f"{head}{line}")
    return rows or [label]


def _tool_output_text(result: dict[str, Any]) -> str:
    """The durable tool result's display text (canonical ``result.output`` shape)."""
    output = result.get("output")
    if isinstance(output, dict):
        output_type = str(output.get("type", ""))
        if output_type == "text":
            return _safe(output.get("text"))
        if output_type == "attachment":
            mime = _safe(output.get("mime") or "attachment")
            return f"[{mime}]"
    return ""


def _notice_row(text: object, tone: str) -> str:
    color = {"ok": "32", "warning": "33", "error": "31"}.get(tone, "36")
    return f"\x1b[{color}m[{_safe(text)}]\x1b[0m"


def _fit(text: str, width: int) -> str:
    """Trim a row to the current terminal width without cutting ANSI in half."""
    return text if visible_width(text) <= width else text[:width]


class KernelTuiApp:
    """Full-featured HTTP-backed TUI running on pure Terminal and LiveRegion."""

    def __init__(
        self,
        client: KernelHttpClient,
        *,
        session_id: str | None = None,
        terminal: Terminal | None = None,
    ) -> None:
        self.client = client
        self.terminal = terminal or Terminal.open()
        self.owns_terminal = terminal is None
        self.session_id = session_id or ""
        self.running = False
        from .theme import resolve_theme

        self.composer = Composer(theme=resolve_theme(), width=self.terminal.width, placeholder="Type a message or /command...")
        self.key_decoder = KeyDecoder()
        self._escape_flush_at = 0.0
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._session_cursors: dict[str, int] = {}
        self._generation = 0
        self.active_overlay: InteractiveChoices | InteractiveConfirmation | InteractiveForm | None = None
        # Native tool approval (core permission gate) is not a plugin Action
        # confirmation: it resumes the parked run with its own request id.
        self.pending_approval: dict[str, Any] | None = None
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
        self.status_line = "Connecting to kernel..."
        self._sse_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

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
            self.status_line = f"Attached session {self.session_id}"
        else:
            # No session requested: list sessions; if none exist, create one
            sessions = self.client.list_sessions(limit=1)
            if sessions:
                actual_id = str(sessions[0].get("session_id") or "")
                if not actual_id:
                    raise RuntimeError("Server returned session without session_id")
                self.session_id = actual_id
                self.status_line = f"Attached session {self.session_id}"
            else:
                new_session = self.client.create_session()
                actual_id = str(new_session.get("session_id") or "")
                if not actual_id:
                    raise RuntimeError("Server created session without session_id")
                self.session_id = actual_id
                self.status_line = f"Created session {self.session_id}"

    def _load_catalog(self) -> None:
        try:
            revision, commands = self.client.command_catalog()
            self.catalog_cache = commands
            self.catalog_revision = revision
        except Exception as exc:
            self.catalog_cache = []
            self.catalog_revision = ""
            self.status_line = f"Catalog load failed: {_safe(exc)}"
            raise RuntimeError(f"Failed to fetch command catalog from kernel transport: {exc}") from exc

    def _start_sse_stream(self) -> None:
        generation = self._generation
        session_id = self.session_id
        after_seq = self._session_cursors.get(session_id, 0)

        def stream_worker() -> None:
            while not self._stop_event.is_set() and generation == self._generation:
                try:
                    for frame in self.client.events_stream(session_id, after_sequence=after_seq, follow=True):
                        if self._stop_event.is_set() or generation != self._generation:
                            return
                        tagged = dict(frame)
                        tagged["generation"] = generation
                        tagged["session_id"] = session_id
                        self.event_queue.put(tagged)
                except Exception as exc:
                    if self._stop_event.is_set() or generation != self._generation:
                        return
                    logger.debug("SSE stream reconnecting after: %s", exc)
                    time.sleep(1.0)

        self._sse_thread = threading.Thread(target=stream_worker, daemon=True, name=f"KernelTuiSSE{generation}")
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
            if "events" in frame and isinstance(frame["events"], list):
                for ev in frame["events"]:
                    self._handle_single_event(ev, rows_to_commit)
            elif kind == "event" and "event" in frame:
                self._handle_single_event(frame["event"], rows_to_commit)
            elif kind == "notice":
                rows_to_commit.append(_notice_row(frame.get("text"), str(frame.get("tone", "info"))))
            elif kind == "approval":
                approval = frame.get("approval")
                if isinstance(approval, dict):
                    self.pending_approval = approval
                    self.status_line = f"Tool approval required: {approval.get('tool_name', 'tool')}"
            elif kind == "run_status":
                status = str(frame.get("status", ""))
                if status == "waiting":
                    self.status_line = "Waiting for tool approval"
                if status in ("failed", "interrupted"):
                    detail = frame.get("error")
                    suffix = f": {_safe(detail)}" if detail else ""
                    rows_to_commit.append(f"\x1b[33m[Run {status}]{suffix}\x1b[0m")
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
            elif kind == "form_load":
                form = frame.get("form")
                if isinstance(form, InteractiveForm) and form is self.active_overlay:
                    data = frame.get("data")
                    if isinstance(data, dict):
                        form.populate_from_load_action(data)
                    error = frame.get("error")
                    if error:
                        form.load_error = str(error)
            elif kind == "error":
                rows_to_commit.append(f"\x1b[31m[Kernel stream error]\x1b[0m {_safe(frame.get('error'))}")

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

        if etype == "kernel.user_message":
            # The durable event is the single source of the prompt row: the
            # composer never commits it optimistically, and duplicates are
            # excluded by the per-session cursor above (not by a first-only flag).
            rows.append(f"\x1b[1;36mUser>\x1b[0m {_safe(payload.get('content'))}")
        elif etype == "kernel.model_turn":
            raw_turn = payload.get("result")
            turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
            output = turn.get("output")
            if isinstance(output, str) and output:
                rows.extend(_prefixed_rows("Assistant>", output, color="32"))
            reasoning = turn.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                rows.extend(_prefixed_rows("Reasoning>", reasoning, color="2;37"))
            tool_calls = turn.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    name = _safe(call.get("tool_name") or "tool")
                    arguments = json.dumps(json_safe(call.get("arguments")), ensure_ascii=False, allow_nan=False)
                    rows.append(f"\x1b[33m▶ Tool call {name}\x1b[0m {_safe(arguments)}")
        elif etype in ("kernel.tool_completed", "kernel.tool_interrupted"):
            raw_call = payload.get("call")
            call: dict[str, Any] = raw_call if isinstance(raw_call, dict) else {}
            raw_result = payload.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
            name = _safe(result.get("tool_name") or "unknown")
            failed = etype == "kernel.tool_interrupted" or result.get("status") == "error"
            body = _tool_output_text(result)
            if failed:
                error = _safe(result.get("error") or body or "interrupted")
                rows.append(f"\x1b[31m✖ Tool {name} failed:\x1b[0m {error}")
            else:
                rows.append(f"\x1b[32m✔ Tool {name}:\x1b[0m {body}")
        elif etype in ("kernel.command_completed", "kernel.command_intent"):
            raw_command_result = payload.get("result")
            command_result: dict[str, Any] = raw_command_result if isinstance(raw_command_result, dict) else {}
            name = _safe(payload.get("name") or "command")
            if etype == "kernel.command_intent":
                rows.append(f"\x1b[36m▷ Command /{name} started\x1b[0m")
            else:
                status = str(command_result.get("status", ""))
                detail = command_result.get("output") or command_result.get("error") or ""
                if status == "error":
                    rows.append(f"\x1b[31m✖ Command /{name} failed:\x1b[0m {_safe(detail)}")
                elif status == "pending_confirmation":
                    rows.append(f"\x1b[33m⏳ Command /{name} awaits confirmation\x1b[0m")
                else:
                    rows.append(f"\x1b[32m✔ Command /{name}:\x1b[0m {_safe(detail)}")
        elif etype == "kernel.activation_committed":
            # The displayed generation changed: the catalog and the views are
            # refetched together, and anything captured under the previous
            # revision is dropped — never relabelled, never retried.
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
            self._show_views()
            self._present_queued_confirmation()
            suffix = " (stale action discarded)" if dropped else ""
            rows.append(f"\x1b[33m[Capability revision changed to {_safe(to_revision_id)}: catalog and views refreshed{suffix}]\x1b[0m")
        elif etype == "kernel.confirmation_requested":
            # Durable pending fact: restores the modal after a reload, bound to
            # the revision that created the request (never the current one).
            request_id = _safe(payload.get("request_id"))
            ui_data = payload.get("ui_data")
            if request_id and str(payload.get("ui_type", "")) == "confirmation" and isinstance(ui_data, dict):
                self._show_or_queue_confirmation(InteractiveConfirmation(ui_data, revision_id=str(event.get("revision_id", ""))))
        elif etype == "kernel.confirmation_decided":
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
        elif etype == "kernel.approval_requested":
            raw_call = payload.get("call")
            call: dict[str, Any] = raw_call if isinstance(raw_call, dict) else {}
            request_id = _safe(payload.get("request_id"))
            if request_id:
                self.pending_approval = {
                    "request_id": request_id,
                    "tool_name": _safe(call.get("tool_name") or "tool"),
                    "arguments": call.get("arguments"),
                }
                self.status_line = f"Tool approval required: {self.pending_approval['tool_name']}"
        elif etype == "kernel.approval_decided":
            decision = _safe(payload.get("decision"))
            rows.append(f"\x1b[33m[Approval {decision}]\x1b[0m")
        elif etype == "kernel.run_interrupted":
            rows.append(f"\x1b[33m[Run interrupted: {_safe(payload.get('stop_cause'))}]\x1b[0m")
        elif etype == "kernel.run_failed":
            rows.append("\x1b[31m[Run failed]\x1b[0m")
        elif etype == "kernel.observer_error":
            rows.append(f"\x1b[31m[Plugin observer error]\x1b[0m {_safe(payload.get('error'))}")

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
            self.status_line = f"Confirmation required: {request_id}"
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
            self.status_line = f"Confirmation required: {request_id}"
            return

    def _process_input(self) -> None:
        now = time.monotonic()
        chunk = self.terminal.read_bytes(timeout=0.001)
        if chunk:
            # A lone ESC is ambiguous until no further bytes follow: the standard
            # disambiguation window resolves it as Esc (never as part of the next
            # byte). The legacy app uses the same 50 ms.
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

        threading.Thread(target=worker, daemon=True, name="KernelCancel").start()

    def _handle_approval_key(self, key: Any) -> None:
        """Native approval gate: a=allow once, d/Esc=deny; both hit /resume."""
        if key.name in ("a", "enter"):
            self._resolve_approval("allow")
        elif key.name in ("d", "esc", "ctrl+c"):
            self._resolve_approval("deny")

    def _resolve_approval(self, decision: str) -> None:
        approval = self.pending_approval
        if not approval:
            return
        request_id = str(approval.get("request_id", ""))
        self.pending_approval = None
        self._present_queued_confirmation()
        if not request_id:
            self.status_line = "No pending approval to resolve"
            return
        session_id, generation = self.session_id, self._generation
        self.status_line = f"Submitting approval {decision}..."

        def worker() -> None:
            try:
                res = self.client.resume(session_id, approval_request_id=request_id, decision=decision)
                status = str(res.get("status", ""))
                self._queue_notice(
                    f"Approval {decision}: {status}",
                    tone="ok" if status != "error" else "error",
                    session_id=session_id,
                    generation=generation,
                )
            except Exception as exc:
                self._queue_notice(f"Approval {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)

        threading.Thread(target=worker, daemon=True, name="KernelApproval").start()

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
            self.active_overlay = None
            self.status_line = f"Dispatching /{action}..."
            self._present_queued_confirmation()
            self._invoke_async(action, arguments if isinstance(arguments, dict) else {}, revision)

    def _resolve_confirmation(self, decision: str) -> None:
        """Submit the stored request id's decision; the overlay never lies about it."""
        request_id = getattr(self.active_overlay, "request_id", "")
        # The revision of the envelope that displayed this confirmation, captured
        # before the overlay is closed.
        revision = getattr(self.active_overlay, "revision_id", "")
        self.active_overlay = None
        self._present_queued_confirmation()
        if not request_id:
            self.status_line = "No pending confirmation to resolve"
            return
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
                status = str(res.get("status", ""))
                self._queue_notice(
                    f"Confirmation {decision}: {status}",
                    tone="ok" if status != "error" else "error",
                    session_id=session_id,
                    generation=generation,
                )
            except Exception as exc:
                self._queue_notice(f"Confirmation {decision} failed: {exc}", tone="error", session_id=session_id, generation=generation)

        threading.Thread(target=worker, daemon=True, name="KernelConfirm").start()

    def _invoke_async(self, name: str, arguments: dict[str, Any], revision: str) -> None:
        "Run one command off the UI thread under the revision the user actually saw."
        session_id, generation = self.session_id, self._generation

        def worker() -> None:
            try:
                res = self.client.invoke_command(
                    session_id,
                    name,
                    arguments,
                    expected_revision=revision,
                )
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
            except Exception as exc:
                self._queue_notice(f"/{name} failed: {exc}", tone="error", session_id=session_id, generation=generation)

        threading.Thread(target=worker, daemon=True, name="KernelInvoke").start()

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
        # No optimistic commit: the durable kernel.user_message event is the one
        # and only source of the prompt row on screen.
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

        threading.Thread(target=run_worker, daemon=True, name="KernelRun").start()

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
            self.terminal.commit_rows([f"\x1b[31mUnknown TUI control: /tui {_safe(local)}\x1b[0m"])
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
        self.status_line = f"Session {session_id}"
        self._start_sse_stream()

    def _create_session(self) -> None:
        try:
            res = self.client.create_session()
            new_id = str(res.get("session_id", ""))
        except Exception as exc:
            self.terminal.commit_rows([f"\x1b[31mFailed to create session: {_safe(exc)}\x1b[0m"])
            return
        if new_id:
            self._switch_session(new_id)

    def _show_help(self) -> None:
        lines = [
            "\x1b[1;34m=== VoidCode Kernel Commands ===\x1b[0m",
            "  /tui help              Show this help (always available)",
            "  /tui sessions          List available sessions",
            "  /tui session <id>      Switch active session",
            "  /tui new               Create new session",
            "  /tui views [name]      Show current plugin views",
            "  /tui cancel            Cancel active session run",
            "  /tui quit              Exit TUI",
            "  (bare /help, /views, ... work too when no plugin command owns the name)",
            "\x1b[1;34m=== Admitted Plugin Commands ===\x1b[0m",
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
            lines = ["\x1b[1;34m=== Active Sessions ===\x1b[0m"]
            for s in sess_list:
                sid = s.get("session_id", "")
                status = s.get("status", "idle")
                turn = s.get("turn", 0)
                marker = " ▶" if sid == self.session_id else "  "
                lines.append(f"{marker} {sid} (status={status}, turn={turn})")
            self.terminal.commit_rows(lines)
        except Exception as exc:
            self.terminal.commit_rows([f"\x1b[31mFailed to list sessions: {exc}\x1b[0m"])

    def _show_views(self, name_filter: str = "") -> None:
        try:
            views_revision, v_list = self.client.views(self.session_id)
            if name_filter:
                wanted = name_filter.strip().lower()
                v_list = [v for v in v_list if wanted in str(v.get("name", "")).lower() or wanted in str(v.get("title", "")).lower()]
            if not v_list:
                self.terminal.commit_rows(["\x1b[33mNo plugin views available for this session.\x1b[0m"])
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
                rows = render_plugin_view(v, self.terminal.width)
                self.terminal.commit_rows(rows)
        except Exception as exc:
            self.terminal.commit_rows([f"\x1b[31mFailed to read views: {exc}\x1b[0m"])

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

        threading.Thread(target=worker, daemon=True, name="KernelFormLoad").start()

    def _dispatch_catalog_command(self, cmd_name: str, arg_str: str) -> None:
        cmd_desc = next((c for c in self._catalog() if c.get("name") == cmd_name), None)
        if cmd_desc is None:
            self.terminal.commit_rows([f"\x1b[31mUnknown command: /{_safe(cmd_name)}\x1b[0m (try /help)"])
            return
        # The revision of the catalog this descriptor was read from.
        revision = self.catalog_revision

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
                    self.terminal.commit_rows([f"\x1b[31mArguments must be valid JSON: {_safe(arg_str)}\x1b[0m"])
                    return
            else:
                if not isinstance(parsed, dict):
                    self.terminal.commit_rows([f"\x1b[31mArguments must be a JSON object: {_safe(arg_str)}\x1b[0m"])
                    return
                arguments = parsed

        self.status_line = f"Dispatching /{cmd_name}..."
        self._invoke_async(cmd_name, arguments, revision)

    def _render_frame(self) -> None:
        width = self.terminal.width
        if self.pending_approval is not None:
            approval = self.pending_approval
            arguments = json.dumps(json_safe(approval.get("arguments")), ensure_ascii=False, allow_nan=False)
            rows = [
                "\x1b[33m*** Tool approval required ***\x1b[0m",
                f"  tool: {_safe(approval.get('tool_name'))}",
                f"  args: {_safe(arguments)[:_WRAP_WIDTH]}",
                "  [a] allow once    [d] deny",
            ]
            status_row = f"\x1b[7m[{self.session_id}] {self.status_line}\x1b[0m"
            frame = TuiFrame(transcript_rows=(), chrome_rows=(*rows, status_row), cursor=None)
            self.terminal.paint_frame(frame)
        elif self.active_overlay is not None:
            overlay_rows = self.active_overlay.render_rows(width)
            status_row = f"\x1b[7m[{self.session_id}] {self.status_line}\x1b[0m"
            frame = TuiFrame(transcript_rows=(), chrome_rows=(*overlay_rows, status_row), cursor=None)
            self.terminal.paint_frame(frame)
        else:
            self.composer.set_width(width)
            status_row = f"\x1b[7m[{self.session_id}] {self.status_line}\x1b[0m"
            composer_rows = self.composer.render(status=status_row)
            cursor = self.composer.cursor_position
            frame = TuiFrame(transcript_rows=(), chrome_rows=tuple(composer_rows), cursor=cursor)
            self.terminal.paint_frame(frame)


def run_tui(server_url: str, *, token: str | None = None, session_id: str | None = None) -> int:
    """Launch the HTTP-backed generic TUI connected to a running kernel."""
    client = KernelHttpClient(server_url, token=token)
    app = KernelTuiApp(client, session_id=session_id)
    return app.run()
