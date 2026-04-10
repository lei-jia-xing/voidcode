from __future__ import annotations

from pathlib import Path
from typing import Literal

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, RichLog, Static

from ..runtime.config import load_runtime_config
from ..runtime.contracts import RuntimeRequest
from ..runtime.events import EventEnvelope
from ..runtime.permission import PermissionDecision
from ..runtime.service import VoidCodeRuntime
from .messages import StreamChunkReceived, StreamCompleted, StreamFailed
from .screens import ApprovalModal, CommandPalette, SessionListModal


class VoidCodeTUI(App[int]):
    ansi_color = True

    CSS = """
    Screen {
        layout: vertical;
        background: ansi_default;
    }
    #main-layout {
        height: 100%;
        width: 100%;
        background: ansi_default;
    }
    #transcript-column {
        width: 3fr;
        height: 100%;
        border-right: solid $accent;
        background: ansi_default;
    }
    #sidebar-column {
        width: 1fr;
        height: 100%;
        padding: 1;
        background: ansi_default;
    }
    #transcript-log {
        height: 1fr;
        border: solid $panel;
        background: ansi_default;
    }
    #current-response {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: ansi_default;
    }
    #composer-input {
        dock: bottom;
        background: ansi_default;
    }
    .sidebar-header {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        background: ansi_default;
    }
    Footer {
        background: ansi_default;
    }
    FooterKey {
        background: $surface;
        color: $text;
    }
    FooterKey .footer-key--key {
        color: $text;
        background: $surface;
        text-style: bold;
    }
    FooterKey .footer-key--description {
        color: $text-muted;
        background: $surface;
    }
    Footer:ansi {
        background: ansi_default;
    }
    Footer:ansi FooterKey {
        background: $surface;
        color: $text;
    }
    Footer:ansi .footer-key--key {
        color: $text;
        background: $surface;
    }
    Footer:ansi .footer-key--description {
        color: $text-muted;
        background: $surface;
    }
    Footer:ansi FooterKey.-command-palette {
        background: $surface;
    }
    """

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
        self._session_titles: dict[str, str] = {}
        self._current_prompt: str | None = None

        leader_key = "alt+x"
        if config.tui:
            if config.tui.leader_key:
                leader_key = config.tui.leader_key
            if config.tui.keymap:
                for k, action in config.tui.keymap.items():
                    self.bind(k, action)

        self.bind(leader_key, "command_palette", description="Command Palette")

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-layout"):
            with Vertical(id="transcript-column"):
                yield RichLog(id="transcript-log", markup=False)
                yield Static("", id="current-response")
                yield Input(placeholder="Ask voidcode...", id="composer-input")
            with VerticalScroll(id="sidebar-column"):
                yield Static("Status", classes="sidebar-header")
                yield Static("Idle", id="status-panel")
                yield Static("Session", classes="sidebar-header")
                yield Static("None", id="session-panel")
                yield Static("Workspace", classes="sidebar-header")
                yield Static("Unknown", id="workspace-panel")
                yield Static("LSP", classes="sidebar-header")
                yield Static("Disabled", id="lsp-panel")
                yield Static("ACP", classes="sidebar-header")
                yield Static("Disabled", id="acp-panel")
                yield Static("Tokens", classes="sidebar-header")
                yield Static("Unavailable", id="tokens-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._set_state("Idle")
        self.query_one("#workspace-panel", Static).update(self.workspace.name)

        lsp_state = self.runtime.current_lsp_state()
        if lsp_state.mode == "managed":
            active_servers = [
                name for name, s in lsp_state.servers.items() if s.status == "running"
            ]
            if active_servers:
                self.query_one("#lsp-panel", Static).update(f"Active: {len(active_servers)}")
            else:
                self.query_one("#lsp-panel", Static).update("No active servers")
        else:
            self.query_one("#lsp-panel", Static).update("Disabled")

        acp_state = self.runtime.current_acp_state()
        if acp_state.mode == "managed":
            self.query_one("#acp-panel", Static).update(acp_state.status.title())
        else:
            self.query_one("#acp-panel", Static).update("Disabled")

        self.query_one("#composer-input", Input).focus()

    def action_command_palette(self) -> None:
        self.push_screen(CommandPalette(), self._handle_command)

    def action_session_new(self) -> None:
        self._handle_command("session: new")

    def action_session_resume(self) -> None:
        self._handle_command("session: resume")

    def _handle_command(self, command: str | None) -> None:
        if command == "session: new":
            self.session_id = None
            self._current_prompt = None
            self._set_state("Idle")
            self.query_one("#session-panel", Static).update("None")
            self.query_one("#transcript-log", RichLog).clear()
            self.query_one("#transcript-log", RichLog).write(
                Text("--- New Session ---", style="bold")
            )
            self.query_one("#composer-input", Input).focus()
        elif command == "session: resume":
            sessions = self.runtime.list_sessions()
            self._session_titles = {s.session.id: s.prompt for s in sessions}

            def _handle_session(session_id: str | None) -> None:
                if session_id:
                    self.session_id = session_id

                    short_id = session_id.removeprefix("session-")[:8]
                    title = short_id
                    if session_id in self._session_titles:
                        title += f" - {self._session_titles[session_id][:30]}"
                    self.query_one("#session-panel", Static).update(title)

                    self.query_one("#transcript-log", RichLog).clear()
                    self.query_one("#transcript-log", RichLog).write(
                        Text(f"--- Resumed Session {short_id} ---", style="bold")
                    )
                    self._set_state("Running")
                    self._set_stream_active(True)
                    self._replay_stream(session_id)
                    self.query_one("#composer-input", Input).focus()

            self.push_screen(SessionListModal(sessions), _handle_session)

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
        self._current_prompt = prompt

        request = RuntimeRequest(prompt=prompt, allocate_session_id=self.session_id is None)
        self._start_stream(request)

    def _write_user_prompt(self, prompt: str) -> None:
        self.query_one("#transcript-log", RichLog).write(Text(f"User: {prompt}"))

    def _write_event_line(self, event: EventEnvelope) -> None:
        if event.event_type not in (
            "graph.tool_request_created",
            "runtime.tool_completed",
            "runtime.approval_requested",
            "runtime.failed",
            "runtime.approval_resolved",
        ):
            return

        self.query_one("#transcript-log", RichLog).write(
            Text(f"EVENT {event.event_type} source={event.source}", style="dim")
        )

    def _write_output_line(self, output: str) -> None:
        self.query_one("#transcript-log", RichLog).write(Text(output))

    def _set_state(self, state: str) -> None:
        self.current_state = state
        self.query_one("#status-panel", Static).update(state)
        current = self.query_one("#current-response", Static)
        if state == "Idle":
            current.update("")
        elif state == "Running":
            current.update("Working...")
        elif state == "Waiting approval":
            current.update("Waiting for approval...")
        elif state == "Completed":
            current.update("")
        elif state == "Failed":
            current.update("Stream failed.")

    def _set_stream_active(self, active: bool) -> None:
        self._stream_active = active
        self.query_one("#composer-input", Input).disabled = (
            active or self.pending_request_id is not None
        )

    @work(thread=True)
    def _replay_stream(self, session_id: str) -> None:
        last_status = "Idle"
        saw_chunk = False
        try:
            for chunk in self.runtime.resume_stream(session_id=session_id):
                saw_chunk = True
                last_status = chunk.session.status
                self.post_message(StreamChunkReceived(chunk))
            if not saw_chunk:
                raise ValueError("runtime stream emitted no chunks")
            self.post_message(StreamCompleted(last_status))
        except Exception as error:
            self.post_message(StreamFailed(error))

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

    def on_stream_chunk_received(self, message: StreamChunkReceived) -> None:
        chunk = message.chunk
        self.session_id = chunk.session.session.id

        if chunk.kind == "event" and chunk.event is not None:
            if self.session_id:
                short_id = self.session_id.removeprefix("session-")[:8]
                title = short_id
                if self.session_id in self._session_titles:
                    title += f" - {self._session_titles[self.session_id][:30]}"
                elif self._current_prompt:
                    title += f" - {self._current_prompt[:30]}"
                self.query_one("#session-panel", Static).update(title)

            event = chunk.event
            self._write_event_line(event)

            if (
                chunk.session.status == "waiting"
                and event.event_type == "runtime.approval_requested"
            ):
                payload = event.payload or {}
                self.pending_request_id = str(payload.get("request_id", ""))
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
            Text(f"Error: {message.error}", style="bold red")
        )
        self.pending_request_id = None
        self._set_state("Failed")
        self._set_stream_active(False)
