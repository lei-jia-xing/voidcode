from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, RichLog, Static

from ..runtime.config import (
    RuntimeTuiConfig,
    RuntimeTuiPreferences,
    RuntimeTuiReadingPreferences,
    RuntimeTuiThemePreferences,
    effective_runtime_tui_preferences,
    load_global_tui_preferences,
    load_runtime_config,
    load_workspace_tui_preferences,
    merge_runtime_tui_preferences,
    save_global_tui_preferences,
)
from ..runtime.contracts import RuntimeRequest
from ..runtime.events import EventEnvelope
from ..runtime.permission import PermissionDecision
from ..runtime.service import VoidCodeRuntime
from .messages import StreamChunkReceived, StreamCompleted, StreamFailed
from .screens import (
    ApprovalModal,
    CommandPalette,
    SessionListModal,
    ThemeModePickerModal,
    ThemePickerModal,
)


class VoidCodeTUI(App[int]):
    CSS = """
    Screen {
        layout: vertical;
        background: $background;
    }
    #main-layout {
        height: 100%;
        width: 100%;
    }
    #transcript-column {
        width: 3fr;
        height: 100%;
        border-right: solid $accent;
        background: $surface;
    }
    #sidebar-column {
        width: 1fr;
        height: 100%;
        padding: 1;
        background: $background;
    }
    #transcript-log {
        height: 1fr;
        border: solid $panel;
        background: $surface;
    }
    #current-response {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #composer-input {
        dock: bottom;
        background: $panel;
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
        self._session_titles: dict[str, str] = {}
        self._current_prompt: str | None = None
        self._global_tui_preferences = load_global_tui_preferences()
        self._workspace_tui_preferences = load_workspace_tui_preferences(workspace)
        self._effective_preferences = RuntimeTuiPreferences()
        self._tui_preferences = self._global_tui_preferences or RuntimeTuiPreferences()

        if self._global_tui_preferences is None and isinstance(config.tui, RuntimeTuiConfig):
            merged_preferences = config.tui.preferences
            if isinstance(merged_preferences, RuntimeTuiPreferences):
                self._effective_preferences = merged_preferences

        leader_key = "alt+x"
        if isinstance(config.tui, RuntimeTuiConfig):
            if config.tui.leader_key:
                leader_key = config.tui.leader_key
            if isinstance(config.tui.keymap, dict):
                for k, action in config.tui.keymap.items():
                    self.bind(k, action)

        self.bind(leader_key, "command_palette", description="Command Palette")

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-layout"):
            with Vertical(id="transcript-column"):
                yield RichLog(id="transcript-log", markup=False, wrap=True)
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
                yield Static("Context", classes="sidebar-header")
                yield Static("Unknown", id="context-panel")
        yield Footer()

    def on_mount(self) -> None:
        self._set_state("Idle")
        self.query_one("#workspace-panel", Static).update(self.workspace.name)
        self._apply_tui_preferences()
        self._update_context_panel(None)

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

        self.query_one("#composer-input", Input).focus()

    def on_unmount(self) -> None:
        self.runtime.__exit__(None, None, None)

    def action_command_palette(self) -> None:
        self.push_screen(CommandPalette(), self._handle_command)

    def action_session_new(self) -> None:
        self._handle_command("session.new")

    def action_session_resume(self) -> None:
        self._handle_command("session.resume")

    def _handle_command(self, command: str | None) -> None:
        if command == "session.new":
            self.session_id = None
            self._current_prompt = None
            self._set_state("Idle")
            self.query_one("#session-panel", Static).update("None")
            self._update_context_panel(None)
            self.query_one("#transcript-log", RichLog).clear()
            self.query_one("#transcript-log", RichLog).write(
                Text("--- New Session ---", style="bold")
            )
            self.query_one("#composer-input", Input).focus()
        elif command == "session.resume":
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
        elif command == "theme.switch":
            self.push_screen(
                ThemePickerModal(self._available_theme_names()), self._handle_theme_selection
            )
        elif command == "theme.mode":
            self.push_screen(ThemeModePickerModal(), self._handle_theme_mode_selection)
        elif command == "view.wrap":
            self._toggle_wrap()
        elif command == "view.sidebar":
            self._toggle_sidebar()

    def _effective_tui_preferences(self) -> RuntimeTuiPreferences:
        return self._effective_preferences

    def _apply_tui_preferences(self) -> RuntimeTuiPreferences:
        merged_preferences = merge_runtime_tui_preferences(
            self._tui_preferences, self._workspace_tui_preferences
        )
        effective = effective_runtime_tui_preferences(merged_preferences)
        if isinstance(effective.theme.name, str) and effective.theme.name in self.available_themes:
            self.theme = effective.theme.name

        wrap = effective.reading.wrap if effective.reading.wrap is not None else True
        self.query_one("#transcript-log", RichLog).wrap = wrap

        collapsed = (
            effective.reading.sidebar_collapsed
            if effective.reading.sidebar_collapsed is not None
            else False
        )
        sidebar = self.query_one("#sidebar-column", VerticalScroll)
        sidebar.display = not collapsed
        self._effective_preferences = RuntimeTuiPreferences(
            theme=effective.theme,
            reading=effective.reading,
        )
        return self._effective_preferences

    def _persist_global_preferences(self) -> None:
        save_global_tui_preferences(self._tui_preferences)

    def _available_theme_names(self) -> list[str]:
        theme_preferences = self._effective_preferences.theme or RuntimeTuiThemePreferences(
            mode="auto"
        )
        mode = theme_preferences.mode
        themes = sorted(self.available_themes.items())
        if mode == "light":
            return [name for name, theme in themes if theme.dark is False]
        if mode == "dark":
            return [name for name, theme in themes if theme.dark is True]
        return [name for name, _theme in themes]

    def _handle_theme_selection(self, theme_name: str | None) -> None:
        if theme_name is None:
            return
        prefs = self._tui_preferences
        theme_prefs = prefs.theme or RuntimeTuiThemePreferences()
        self._tui_preferences = RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name=theme_name, mode=theme_prefs.mode),
            reading=prefs.reading,
        )
        self._apply_tui_preferences()
        self._persist_global_preferences()

    def _handle_theme_mode_selection(self, mode: str | None) -> None:
        if mode is None:
            return
        prefs = self._tui_preferences
        theme_prefs = prefs.theme or RuntimeTuiThemePreferences()
        self._tui_preferences = RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(
                name=theme_prefs.name, mode=cast(Literal["auto", "light", "dark"], mode)
            ),
            reading=prefs.reading,
        )
        self._apply_tui_preferences()
        self._persist_global_preferences()

    def _toggle_wrap(self) -> None:
        prefs = self._tui_preferences
        effective = self._effective_preferences
        reading_prefs = prefs.reading or RuntimeTuiReadingPreferences()
        effective_reading = effective.reading or RuntimeTuiReadingPreferences(wrap=True)
        self._tui_preferences = RuntimeTuiPreferences(
            theme=prefs.theme,
            reading=RuntimeTuiReadingPreferences(
                wrap=not (effective_reading.wrap if effective_reading.wrap is not None else True),
                sidebar_collapsed=reading_prefs.sidebar_collapsed,
            ),
        )
        self._apply_tui_preferences()
        self._persist_global_preferences()

    def _toggle_sidebar(self) -> None:
        prefs = self._tui_preferences
        effective = self._effective_preferences
        reading_prefs = prefs.reading or RuntimeTuiReadingPreferences()
        effective_reading = effective.reading or RuntimeTuiReadingPreferences(
            sidebar_collapsed=False
        )
        self._tui_preferences = RuntimeTuiPreferences(
            theme=prefs.theme,
            reading=RuntimeTuiReadingPreferences(
                wrap=reading_prefs.wrap,
                sidebar_collapsed=not (
                    effective_reading.sidebar_collapsed
                    if effective_reading.sidebar_collapsed is not None
                    else False
                ),
            ),
        )
        self._apply_tui_preferences()
        self._persist_global_preferences()

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

        payload = event.payload or {}
        tool_name = payload.get("tool", "unknown_tool")

        if event.event_type == "graph.tool_request_created":
            text = Text(f"▶ Started tool: {tool_name}", style="bold blue")
        elif event.event_type == "runtime.tool_completed":
            text = Text(f"✔ Completed tool: {tool_name}", style="bold green")
        elif event.event_type == "runtime.approval_requested":
            text = Text(f"⚠ Approval requested for tool: {tool_name}", style="bold yellow")
        elif event.event_type == "runtime.approval_resolved":
            decision = payload.get("decision", "unknown")
            text = Text(f"ℹ Approval {decision} for tool: {tool_name}", style="bold cyan")
        elif event.event_type == "runtime.failed":
            error_msg = payload.get("error_summary", payload.get("error", "Unknown error"))
            formatted_error = self._format_runtime_error(error_msg)
            text = Text(f"✖ Failed: {formatted_error}", style="bold red")
        else:
            text = Text(f"EVENT {event.event_type} source={event.source}", style="dim")

        self.query_one("#transcript-log", RichLog).write(text)

    def _write_output_line(self, output: str) -> None:
        self.query_one("#transcript-log", RichLog).write(Markdown(output))

    @staticmethod
    def _format_runtime_error(error: object) -> str:
        if not isinstance(error, str):
            return "Unknown error"
        cleaned = error.removeprefix("Error: ").strip()
        for prefix in ("Runtime failed:", "runtime failed:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
        return cleaned or error

    @staticmethod
    def _context_int_value(context_window: dict[str, object], key: str, default: int = 0) -> int:
        value = context_window.get(key, default)
        return value if isinstance(value, int) else default

    @staticmethod
    def _context_str_value(
        context_window: dict[str, object], key: str, default: str = "unknown"
    ) -> str:
        value = context_window.get(key, default)
        return value if isinstance(value, str) else default

    def _update_context_panel(self, metadata: dict[str, object] | None) -> None:
        context_panel = self.query_one("#context-panel", Static)
        if not metadata or "context_window" not in metadata:
            context_panel.update("Unknown")
            return

        cw = metadata["context_window"]
        if not isinstance(cw, dict):
            context_panel.update("Unknown")
            return
        context_window = cast(dict[str, object], cw)

        retained = self._context_int_value(context_window, "retained_tool_result_count")
        max_count = self._context_int_value(context_window, "max_tool_result_count")

        if max_count > 0:
            pct = int((retained / max_count) * 100)
            text = f"{retained} / {max_count} results ({pct}%)"
        else:
            text = f"{retained} results"

        if (
            self._context_int_value(context_window, "compacted", 0)
            or context_window.get("compacted") is True
        ):
            reason = self._context_str_value(context_window, "compaction_reason")
            text += f"\n[Compacted: {reason}]"

        context_panel.update(text)

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

        if hasattr(chunk.session, "metadata") and chunk.session.metadata:
            self._update_context_panel(chunk.session.metadata)

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
            Text(f"Error: {self._format_runtime_error(message.error)}", style="bold red")
        )
        self.pending_request_id = None
        self._set_state("Failed")
        self._set_stream_active(False)
