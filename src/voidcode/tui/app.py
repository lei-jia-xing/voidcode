from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal, cast

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from ..runtime.config import load_runtime_config
from ..runtime.contracts import RuntimeRequest
from ..runtime.events import EventEnvelope
from ..runtime.permission import PermissionDecision
from ..runtime.service import VoidCodeRuntime
from ..runtime.session import StoredSessionSummary
from .messages import StreamChunkReceived, StreamCompleted, StreamFailed
from .screens import ApprovalModal, RuntimeDetailScreen

# Internal events that carry no value to the operator; suppress from transcript.
_SILENT_EVENT_TYPES = frozenset(
    {
        "runtime.request_received",
        "runtime.permission_resolved",
        "runtime.tool_lookup_succeeded",
        "runtime.tool_hook_pre",
        "runtime.tool_hook_post",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.response_ready",
    }
)

_STATUS_ICONS: dict[str, str] = {
    "completed": "●",
    "failed": "✗",
    "waiting": "◐",
    "running": "◌",
    "idle": "○",
}
_LSP_ICONS: dict[str, str] = {
    "running": "●",
    "starting": "◌",
    "stopped": "○",
    "failed": "✗",
}
_ACP_ICONS: dict[str, str] = {
    "connected": "●",
    "disconnected": "○",
    "failed": "✗",
}


class StatusBar(Static):
    """Compact single-line runtime status bar docked at the bottom."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    """


class VoidCodeTUI(App[int]):
    TITLE = "VoidCode"
    ANSI_COLOR = True
    COMMAND_PALETTE_BINDING: ClassVar[str] = ""  # type: ignore[assignment]  # disable built-in Ctrl+P

    CSS = """
    Screen {
        layout: vertical;
        background: transparent;
    }
    #main-layout {
        height: 1fr;
        width: 100%;
    }
    #transcript-log {
        height: 1fr;
    }
    #status-panel {
        min-height: 1;
        max-height: 3;
        border-top: solid $panel;
        padding: 0 1;
        color: $text-muted;
    }
    #composer-input {
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("ctrl+p", "open_sessions", "Sessions"),
        Binding("ctrl+x", "open_detail", "Runtime", priority=True),
    ]

    def __init__(self, workspace: Path, approval_mode: PermissionDecision | None = None) -> None:
        super().__init__()
        self.workspace = workspace
        self.approval_mode = approval_mode
        config = load_runtime_config(workspace, approval_mode=approval_mode)
        self.runtime = VoidCodeRuntime(workspace=workspace, config=config)
        self.session_id: str | None = None
        self.pending_request_id: str | None = None
        self.current_state = "Idle"
        self._stream_active = False
        self._tokens_in: int = 0
        self._tokens_out: int = 0

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main-layout"):
            yield RichLog(id="transcript-log", markup=False, highlight=False)
            yield Static("Idle", id="status-panel")
            yield Input(placeholder="Ask voidcode…", id="composer-input")
        yield StatusBar(id="runtime-status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self._set_state("Idle")
        self.query_one("#composer-input", Input).focus()
        self._refresh_status_bar()

    # ── input ─────────────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return
        if self._stream_active or self.pending_request_id is not None:
            return

        event.input.value = ""
        self._write_user_prompt(prompt)
        self._set_state("Running")
        self._set_stream_active(True)

        if self.session_id is None:
            self._tokens_in = 0
            self._tokens_out = 0

        request = RuntimeRequest(prompt=prompt, allocate_session_id=self.session_id is None)
        self._start_stream(request)

    # ── transcript helpers ────────────────────────────────────────────────────

    def _write_user_prompt(self, prompt: str) -> None:
        self.query_one("#transcript-log", RichLog).write(Text(f"You: {prompt}", style="bold"))

    def _write_event_line(self, event: EventEnvelope) -> None:
        if event.event_type in _SILENT_EVENT_TYPES:
            return

        log = self.query_one("#transcript-log", RichLog)
        p = event.payload

        if event.event_type == "runtime.skills_applied":
            skills = p.get("skills")
            skill_list = cast(list[object], skills) if isinstance(skills, list) else []
            names = ", ".join(str(s) for s in skill_list)
            log.write(Text(f"⚡ skills: {names}", style="dim"))

        elif event.event_type == "graph.model_turn":
            provider = p.get("provider") or ""
            model = p.get("model") or ""
            tag = " / ".join(x for x in (str(provider), str(model)) if x)
            log.write(Text(f"◌ {tag or 'model turn'}", style="dim"))

        elif event.event_type == "graph.tool_request_created":
            tool = p.get("tool", "")
            path = p.get("path", "")
            suffix = f"  {path}" if path else ""
            log.write(Text(f"→ {tool}{suffix}", style="cyan"))

        elif event.event_type == "runtime.tool_completed":
            # Covered by the "→ tool" line above; suppress duplicate output.
            pass

        elif event.event_type == "runtime.approval_resolved":
            decision = p.get("decision", "")
            icon, style = ("✓", "green") if decision == "allow" else ("✗", "red")
            log.write(Text(f"{icon} approval: {decision}", style=style))

        elif event.event_type == "runtime.failed":
            log.write(Text(f"✗ {p.get('error', 'unknown error')}", style="bold red"))

        elif event.event_type in {
            "runtime.lsp_server_started",
            "runtime.lsp_server_stopped",
            "runtime.lsp_server_failed",
        }:
            server = p.get("server", "")
            state = p.get("state", "")
            log.write(Text(f"LSP  {server}: {state}", style="dim"))

        elif event.event_type in {
            "runtime.acp_connected",
            "runtime.acp_disconnected",
            "runtime.acp_failed",
        }:
            status = p.get("status", "")
            log.write(Text(f"ACP: {status}", style="dim"))

        else:
            # Unknown event — show generically per contract requirement.
            log.write(Text(f"· {event.event_type}", style="dim"))

    def _write_output_line(self, output: str) -> None:
        self.query_one("#transcript-log", RichLog).write(Text(output))

    # ── state management ──────────────────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        self.current_state = state
        self.query_one("#status-panel", Static).update(state)
        self._refresh_status_bar()

    def _set_stream_active(self, active: bool) -> None:
        self._stream_active = active
        self.query_one("#composer-input", Input).disabled = (
            active or self.pending_request_id is not None
        )

    # ── status bar ────────────────────────────────────────────────────────────

    def _refresh_status_bar(self) -> None:
        parts: list[str] = []

        # Workspace
        parts.append(self.workspace.name or str(self.workspace))

        # Session + state
        if self.session_id:
            short_id = self.session_id[:8]
            icon = _STATUS_ICONS.get(self.current_state.lower(), "○")
            parts.append(f"{short_id}  {icon} {self.current_state}")
        else:
            parts.append("new session")

        # Model
        try:
            model: object = self.runtime._config.model or self.runtime._config.execution_engine  # type: ignore[reportPrivateUsage]
            model_str = str(model)
            if len(model_str) > 24:
                model_str = model_str[:22] + "…"
            parts.append(model_str)
        except Exception:
            pass

        # LSP
        try:
            lsp = self.runtime.current_lsp_state()
            if lsp.mode != "disabled" and lsp.servers:
                lsp_parts = [
                    f"{_LSP_ICONS.get(srv.status, '○')} {name}" for name, srv in lsp.servers.items()
                ]
                parts.append("  ".join(lsp_parts))
        except Exception:
            pass

        # ACP
        try:
            acp = self.runtime.current_acp_state()
            if acp.mode != "disabled":
                icon = _ACP_ICONS.get(acp.status, "○")
                parts.append(f"{icon} MCP")
        except Exception:
            pass

        # Tokens
        if self._tokens_in or self._tokens_out:

            def _fmt(n: int) -> str:
                return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

            parts.append(f"↑{_fmt(self._tokens_in)} ↓{_fmt(self._tokens_out)}")

        bar = self.query_one("#runtime-status-bar", StatusBar)
        bar.update("  ·  ".join(parts))

    def _build_runtime_snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {}
        try:
            snapshot["model"] = self.runtime._config.model  # type: ignore[reportPrivateUsage]
            snapshot["execution_engine"] = self.runtime._config.execution_engine  # type: ignore[reportPrivateUsage]
            snapshot["approval_mode"] = self.runtime._config.approval_mode  # type: ignore[reportPrivateUsage]
        except Exception:
            pass
        try:
            snapshot["tools_count"] = len(self.runtime._tool_registry.definitions())  # type: ignore[reportPrivateUsage]
        except Exception:
            pass
        try:
            lsp = self.runtime.current_lsp_state()
            snapshot["lsp_servers"] = [(name, srv.status) for name, srv in lsp.servers.items()]
        except Exception:
            snapshot["lsp_servers"] = []
        try:
            acp = self.runtime.current_acp_state()
            snapshot["acp_status"] = acp.status
        except Exception:
            snapshot["acp_status"] = "—"
        return snapshot

    # ── stream workers ────────────────────────────────────────────────────────

    @work(thread=True)
    def _start_stream(self, request: RuntimeRequest) -> None:
        last_status = "Idle"
        saw_chunk = False
        try:
            for chunk in self.runtime.run_stream(request):
                saw_chunk = True
                last_status = chunk.session.status
                self.post_message(StreamChunkReceived(chunk))
            if not saw_chunk:
                raise ValueError("runtime stream emitted no chunks")
            self.post_message(StreamCompleted(last_status))
        except Exception as error:
            self.post_message(StreamFailed(error))

    @work(thread=True)
    def _resume_stream(
        self, session_id: str, request_id: str, decision: Literal["allow", "deny"]
    ) -> None:
        last_status = "Idle"
        saw_chunk = False
        try:
            for chunk in self.runtime.resume_stream(
                session_id=session_id,
                approval_request_id=request_id,
                approval_decision=decision,
            ):
                saw_chunk = True
                last_status = chunk.session.status
                self.post_message(StreamChunkReceived(chunk))
            if not saw_chunk:
                raise ValueError("runtime stream emitted no chunks")
            self.post_message(StreamCompleted(last_status))
        except Exception as error:
            self.post_message(StreamFailed(error))

    @work(thread=True)
    def _replay_session(self, summary: StoredSessionSummary) -> None:
        last_status = "Idle"
        saw_chunk = False
        try:
            for chunk in self.runtime.resume_stream(summary.session.id):
                saw_chunk = True
                last_status = chunk.session.status
                self.post_message(StreamChunkReceived(chunk))
            if not saw_chunk:
                raise ValueError("runtime stream emitted no chunks")
            self.post_message(StreamCompleted(last_status))
        except Exception as error:
            self.post_message(StreamFailed(error))

    # ── message handlers ──────────────────────────────────────────────────────

    def on_stream_chunk_received(self, message: StreamChunkReceived) -> None:
        chunk = message.chunk
        self.session_id = chunk.session.session.id

        if chunk.kind == "event" and chunk.event is not None:
            event = chunk.event

            # Accumulate token counts when available in the event payload.
            if event.event_type == "graph.model_turn":
                raw_in = event.payload.get("input_tokens") or 0
                raw_out = event.payload.get("output_tokens") or 0
                self._tokens_in += raw_in if isinstance(raw_in, int) else 0
                self._tokens_out += raw_out if isinstance(raw_out, int) else 0

            self._write_event_line(event)

            if (
                chunk.session.status == "waiting"
                and event.event_type == "runtime.approval_requested"
            ):
                self.pending_request_id = str(event.payload["request_id"])
                self._set_state("Waiting approval")
                self._set_stream_active(False)

                def _handle_decision(decision: Literal["allow", "deny"] | None) -> None:
                    if decision is None:
                        decision = "deny"
                    if self.session_id is None or self.pending_request_id is None:
                        return
                    request_id = self.pending_request_id
                    self.pending_request_id = None
                    self._set_state("Running")
                    self._set_stream_active(True)
                    self._resume_stream(self.session_id, request_id, decision)

                self.push_screen(ApprovalModal(event), _handle_decision)
                return

            # Refresh status bar on runtime infrastructure events.
            if event.event_type in {
                "runtime.lsp_server_started",
                "runtime.lsp_server_stopped",
                "runtime.lsp_server_failed",
                "runtime.acp_connected",
                "runtime.acp_disconnected",
                "runtime.acp_failed",
            }:
                self._refresh_status_bar()
                return

            if event.event_type == "runtime.failed":
                self._set_state("Failed")
            elif chunk.session.status == "running":
                self._set_state("Running")
            elif chunk.session.status == "completed":
                self._set_state("Completed")

        elif chunk.kind == "output" and chunk.output is not None:
            self._write_output_line(chunk.output)
            self._set_state("Completed")

    def on_stream_completed(self, message: StreamCompleted) -> None:
        if message.final_status == "waiting":
            self._set_state("Waiting approval")
            self._set_stream_active(False)
            return
        if message.final_status == "failed":
            self._set_state("Failed")
        else:
            self._set_state("Idle")
        self._set_stream_active(False)

    def on_stream_failed(self, message: StreamFailed) -> None:
        self.query_one("#transcript-log", RichLog).write(
            Text(f"✗ {message.error}", style="bold red")
        )
        self.pending_request_id = None
        self._set_state("Failed")
        self._set_stream_active(False)

    # ── session / detail picker ───────────────────────────────────────────────

    def action_open_sessions(self) -> None:
        if self._stream_active:
            self.notify("Cannot switch sessions while running.", severity="warning")
            return
        self._open_detail(initial_tab="sessions")

    def action_open_detail(self) -> None:
        if self._stream_active:
            self.notify("Cannot open runtime details while running.", severity="warning")
            return
        self._open_detail(initial_tab="sessions")

    def _open_detail(self, *, initial_tab: str = "sessions") -> None:
        sessions = self.runtime.list_sessions()
        snapshot = self._build_runtime_snapshot()

        def _handle_selection(summary: StoredSessionSummary | None) -> None:
            if summary is None or self._stream_active:
                return
            self.query_one("#transcript-log", RichLog).clear()
            self.session_id = summary.session.id
            self._tokens_in = 0
            self._tokens_out = 0
            self._write_user_prompt(f"[resume {summary.session.id[:8]}]")
            self._set_state("Running")
            self._set_stream_active(True)
            self._replay_session(summary)

        self.push_screen(
            RuntimeDetailScreen(sessions, snapshot, initial_tab=initial_tab),
            _handle_selection,
        )
