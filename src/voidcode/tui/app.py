from __future__ import annotations

from pathlib import Path
from typing import Literal

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, RichLog, Static

from ..runtime.config import load_runtime_config
from ..runtime.contracts import RuntimeRequest
from ..runtime.events import EventEnvelope
from ..runtime.permission import PermissionDecision
from ..runtime.service import VoidCodeRuntime
from .messages import StreamChunkReceived, StreamCompleted, StreamFailed
from .screens import ApprovalModal


class VoidCodeTUI(App[int]):
    TITLE = "VoidCode TUI"
    CSS = """
    Screen {
        layout: vertical;
    }
    #main-layout {
        height: 100%;
        width: 100%;
    }
    #transcript-column {
        width: 3fr;
        height: 100%;
        border-right: solid $accent;
    }
    #sidebar-column {
        width: 1fr;
        height: 100%;
        padding: 1;
    }
    #transcript-log {
        height: 1fr;
        border: solid $panel;
    }
    #current-response {
        min-height: 3;
        max-height: 8;
        border: solid $success;
        padding: 0 1;
    }
    #composer-input {
        dock: bottom;
    }
    .sidebar-header {
        text-style: bold;
        color: $accent;
        margin-top: 1;
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

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            with Vertical(id="transcript-column"):
                yield RichLog(id="transcript-log", markup=False)
                yield Static("Waiting for input...", id="current-response")
                yield Input(placeholder="Ask voidcode...", id="composer-input")
            with VerticalScroll(id="sidebar-column"):
                yield Static("Status", classes="sidebar-header")
                yield Static("Idle", id="status-panel")
                yield Static("Tasks", classes="sidebar-header")
                yield Static("No active tasks.", id="tasks-panel")
                yield Static("MCP", classes="sidebar-header")
                yield Static("No tools loaded.", id="mcp-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._set_state("Idle")
        self.query_one("#composer-input", Input).focus()

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

        request = RuntimeRequest(prompt=prompt, allocate_session_id=self.session_id is None)
        self._start_stream(request)

    def _write_user_prompt(self, prompt: str) -> None:
        self.query_one("#transcript-log", RichLog).write(Text(f"User: {prompt}"))

    def _write_event_line(self, event: EventEnvelope) -> None:
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
            current.update("Waiting for input...")
        elif state == "Running":
            current.update("Working...")
        elif state == "Waiting approval":
            current.update("Waiting for approval...")
        elif state == "Completed":
            current.update("Completed. Waiting for input...")
        elif state == "Failed":
            current.update("Stream failed. Waiting for input...")

    def _set_stream_active(self, active: bool) -> None:
        self._stream_active = active
        self.query_one("#composer-input", Input).disabled = (
            active or self.pending_request_id is not None
        )

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
            event = chunk.event
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
