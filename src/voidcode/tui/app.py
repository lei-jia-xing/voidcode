from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, Static

from ..runtime.config import (
    RuntimeConfig,
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
from ..runtime.contracts import CommandSummary, RuntimeRequest, RuntimeStreamChunk
from ..runtime.events import EventEnvelope
from ..runtime.lsp import LspManagerState
from ..runtime.permission import PermissionDecision, PermissionResolution
from ..runtime.question import QuestionResponse
from ..runtime.service import VoidCodeRuntime
from ..runtime.session import StoredSessionSummary
from .messages import (
    ContextPanelUpdated,
    ParentSessionEventsPolled,
    StreamChunkReceived,
    StreamCompleted,
    StreamFailed,
)
from .screens import (
    ApprovalModal,
    QuestionModal,
    SessionListModal,
    ThemeModePickerModal,
    ThemePickerModal,
)
from .timeline import TimelineView

logger = logging.getLogger(__name__)

_SLASH_COMMANDS: tuple[str, ...] = ("/expand",)


@runtime_checkable
class RuntimeProtocol(Protocol):
    """Runtime surface consumed by the TUI.

    Structural contract matching the subset of ``VoidCodeRuntime`` public
    methods used by ``VoidCodeTUI``. It is the injection seam that lets tests
    (and future clients) pass a mock or alternate runtime instead of forcing a
    real ``VoidCodeRuntime`` construction.
    """

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...

    def queue_steering(self, session_id: str, content: str) -> tuple[dict[str, object], ...]: ...

    def resume_stream(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> Iterator[RuntimeStreamChunk]: ...

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]: ...

    def answer_question_stream(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> Iterator[RuntimeStreamChunk]: ...

    def current_lsp_state(self) -> LspManagerState: ...

    def read_tool_output_artifact(
        self,
        *,
        session_id: str,
        artifact_id: str | None = None,
        tool_call_id: str | None = None,
        offset: int = 0,
        limit: int = 2000,
    ) -> dict[str, object]: ...

    def list_command_summaries(self) -> tuple[CommandSummary, ...]: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


class _ComposerInput(Input):
    """Composer with keyboard accessibility helpers.

    - ``Tab`` completes an in-progress slash command (e.g. ``/exp`` -> ``/expand``).
    - ``Shift+Tab`` moves focus out of the composer so the user can reach other widgets.
    """

    def on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            if self.value.startswith("/"):
                completed = self._complete_slash_command(self.value)
                if completed is not None:
                    self.value = completed
                    self.cursor_position = len(completed)
            event.stop()
        elif event.key == "shift+tab":
            event.stop()
            self.screen.focus_next()

    @staticmethod
    def _complete_slash_command(value: str) -> str | None:
        head, _, rest = value.partition(" ")
        if not head.startswith("/"):
            return None
        candidates = [cmd for cmd in _SLASH_COMMANDS if cmd.startswith(head.lower())]
        if len(candidates) == 1:
            return candidates[0] + (" " + rest if rest else "")
        return None


class VoidCodeTUI(App[int]):
    BINDINGS = [("ctrl+o", "tools_expand", "Expand tools")]

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
    .timeline-entry {
        margin: 0 1;
        padding: 0 1;
    }
    .user-message {
        margin: 1 1 0 1;
        padding: 0 1;
        border-left: thick $secondary;
        background: $panel;
        color: $text;
    }
    .timeline-block {
        margin: 0 1;
        padding: 0;
    }
    .timeline-block-content {
        padding: 0 1 1 2;
        color: $text-muted;
    }
    .tool-running {
        color: $accent;
    }
    .tool-pending {
        color: $text-muted;
    }
    .tool-success {
        color: $success;
    }
    .tool-error {
        color: $error;
    }
    .thinking-block {
        color: $text-muted;
        border-left: tall $primary-muted;
    }
    .assistant-stream {
        margin: 1 1 0 1;
        border-left: tall $accent;
        padding: 0 1;
        background: $surface;
        color: $text;
    }
    #current-response {
        height: auto;
        max-height: 12;
        overflow-y: auto;
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

    def __init__(
        self,
        workspace: Path,
        approval_mode: PermissionDecision | None = None,
        *,
        runtime: RuntimeProtocol | None = None,
        tui_preferences: RuntimeTuiPreferences | None = None,
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.approval_mode = approval_mode
        config = load_runtime_config(workspace, approval_mode=approval_mode)
        if runtime is None:
            runtime = VoidCodeRuntime(workspace=workspace, config=config)
        self.runtime = runtime
        self.session_id: str | None = None
        self.pending_request_id: str | None = None
        self.pending_question_request_id: str | None = None
        self.current_state = "Idle"
        self._stream_active = False
        self._session_titles: dict[str, str] = {}
        self._current_prompt: str | None = None
        self._global_tui_preferences = load_global_tui_preferences()
        self._workspace_tui_preferences = load_workspace_tui_preferences(workspace)
        self._effective_preferences = RuntimeTuiPreferences()
        self._tui_preferences = tui_preferences if tui_preferences is not None else (self._global_tui_preferences or RuntimeTuiPreferences())
        self._pending_tool_progress: dict[str, dict[str, list[str]]] = {}
        self._tool_display_by_call_id: dict[str, dict[str, object]] = {}
        self._tool_content_by_call_id: dict[str, str] = {}
        self._tool_artifact_by_call_id: dict[str, str] = {}
        self._approval_context_by_request_id: dict[str, dict[str, object]] = {}
        self._pending_output: list[str] = []
        self._stream_output_buffer = ""
        self._thinking_buffer = ""
        self._streamed_provider_text = False
        self._preview_flush_scheduled = False
        self._stream_render_counter = 0
        self._active_thinking_key: str | None = None
        self._active_response_key: str | None = None
        self._last_event_sequence_by_session: dict[str, int] = {}
        self._background_event_poll_active = False
        self._tracked_background_task_ids: set[str] = set()
        self._background_poll_timer_scheduled = False

        if self._global_tui_preferences is None and isinstance(config.tui, RuntimeTuiConfig):
            merged_preferences = config.tui.preferences
            if isinstance(merged_preferences, RuntimeTuiPreferences):
                self._effective_preferences = merged_preferences

        self._configure_keybindings(config)

    def _configure_keybindings(self, config: RuntimeConfig) -> None:
        if isinstance(config.tui, RuntimeTuiConfig):
            if isinstance(config.tui.keymap, dict):
                for k, action in config.tui.keymap.items():
                    self.bind(k, action)

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-layout"):
            with Vertical(id="transcript-column"):
                transcript_log = TimelineView(id="transcript-log")
                transcript_log.tooltip = "Session transcript; Tab completes /commands, Shift+Tab moves focus out of the composer"
                yield transcript_log
                yield Static("", id="current-response")
                yield _ComposerInput(
                    placeholder="Ask voidcode...",
                    id="composer-input",
                    tooltip=("Ask a question or type /expand <tool_call_id>. Tab completes slash commands; Shift+Tab exits the composer."),
                )
            with VerticalScroll(id="sidebar-column"):
                yield Static("Status", classes="sidebar-header")
                status_panel = Static("Idle", id="status-panel")
                status_panel.tooltip = "Current runtime state"
                yield status_panel
                yield Static("Session", classes="sidebar-header")
                session_panel = Static("None", id="session-panel")
                session_panel.tooltip = "Active session id and prompt"
                yield session_panel
                yield Static("Workspace", classes="sidebar-header")
                workspace_panel = Static("Unknown", id="workspace-panel")
                workspace_panel.tooltip = "Workspace directory in use"
                yield workspace_panel
                yield Static("LSP", classes="sidebar-header")
                lsp_panel = Static("Disabled", id="lsp-panel")
                lsp_panel.tooltip = "Language server status"
                yield lsp_panel
                yield Static("Context", classes="sidebar-header")
                context_panel = Static("Unknown", id="context-panel")
                context_panel.tooltip = "Retained context and token budget"
                yield context_panel
        yield Footer()

    def on_mount(self) -> None:
        self._set_state("Idle")
        self.query_one("#workspace-panel", Static).update(self.workspace.name)
        self._apply_tui_preferences()
        self._update_context_panel(None)

        try:
            lsp_state = self.runtime.current_lsp_state()
        except Exception as exc:
            logger.error("Failed to query LSP state: %s", exc)
            self.query_one("#lsp-panel", Static).update("LSP err")
        else:
            if lsp_state.mode == "managed":
                active_servers = [name for name, s in lsp_state.servers.items() if s.status == "running"]
                if active_servers:
                    self.query_one("#lsp-panel", Static).update(f"Active: {len(active_servers)}")
                else:
                    self.query_one("#lsp-panel", Static).update("No active servers")
            else:
                self.query_one("#lsp-panel", Static).update("Disabled")

        self.query_one("#composer-input", Input).focus()

    def on_unmount(self) -> None:
        try:
            self.runtime.__exit__(None, None, None)
        except Exception as exc:
            logger.error("Failed to shut down runtime: %s", exc)

    def action_session_new(self) -> None:
        self._handle_command("session.new")

    def action_session_resume(self) -> None:
        self._handle_command("session.resume")

    def action_tools_expand(self) -> None:
        timeline = self.query_one("#transcript-log", TimelineView)
        expanded = timeline.toggle_all_blocks()
        self.notify("Tool details expanded" if expanded else "Tool details collapsed")

    def _reset_transient_view_state(self) -> None:
        self.pending_request_id = None
        self.pending_question_request_id = None
        self._approval_context_by_request_id.clear()
        self._pending_tool_progress.clear()
        self._tool_display_by_call_id.clear()
        self._tool_content_by_call_id.clear()
        self._tool_artifact_by_call_id.clear()
        self._tracked_background_task_ids.clear()
        self._pending_output.clear()
        self._stream_output_buffer = ""
        self._thinking_buffer = ""
        self._active_thinking_key = None
        self._active_response_key = None

    def _handle_command(self, command: str | None) -> None:
        if command == "session.new":
            self._reset_transient_view_state()
            self.session_id = None
            self._current_prompt = None
            self._set_state("Idle")
            self.query_one("#session-panel", Static).update("None")
            self._update_context_panel(None)
            self.query_one("#transcript-log", TimelineView).clear()
            self.query_one("#transcript-log", TimelineView).write(Text("--- New Session ---", style="bold"))
            self.query_one("#composer-input", Input).focus()
        elif command == "session.resume":
            try:
                sessions = self.runtime.list_sessions()
            except Exception as exc:
                logger.error("Failed to list sessions: %s", exc)
                sessions = ()
            self._session_titles = {s.session.id: s.prompt for s in sessions}

            def _handle_session(session_id: str | None) -> None:
                if session_id:
                    self._reset_transient_view_state()
                    self.session_id = session_id
                    self._last_event_sequence_by_session[session_id] = 0

                    short_id = session_id.removeprefix("session-")[:8]
                    title = short_id
                    if session_id in self._session_titles:
                        title += f" - {self._session_titles[session_id][:30]}"
                    self.query_one("#session-panel", Static).update(title)

                    self.query_one("#transcript-log", TimelineView).clear()
                    self.query_one("#transcript-log", TimelineView).write(Text(f"--- Resumed Session {short_id} ---", style="bold"))
                    self._set_state("Running")
                    self._set_stream_active(True)
                    self._replay_stream(session_id)
                    self.query_one("#composer-input", Input).focus()

            self.push_screen(SessionListModal(sessions), _handle_session)
        elif command == "theme.switch":
            self.push_screen(ThemePickerModal(self._available_theme_names()), self._handle_theme_selection)
        elif command == "theme.mode":
            self.push_screen(ThemeModePickerModal(), self._handle_theme_mode_selection)
        elif command == "view.wrap":
            self._toggle_wrap()
        elif command == "view.sidebar":
            self._toggle_sidebar()

    def _effective_tui_preferences(self) -> RuntimeTuiPreferences:
        return self._effective_preferences

    def _apply_tui_preferences(self) -> RuntimeTuiPreferences:
        merged_preferences = merge_runtime_tui_preferences(self._tui_preferences, self._workspace_tui_preferences)
        effective = effective_runtime_tui_preferences(merged_preferences)
        if isinstance(effective.theme.name, str) and effective.theme.name in self.available_themes:
            self.theme = effective.theme.name

        wrap = effective.reading.wrap if effective.reading.wrap is not None else True
        self.query_one("#transcript-log", TimelineView).wrap = wrap

        collapsed = effective.reading.sidebar_collapsed if effective.reading.sidebar_collapsed is not None else False
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
        theme_preferences = self._effective_preferences.theme or RuntimeTuiThemePreferences(mode="auto")
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
            theme=RuntimeTuiThemePreferences(name=theme_prefs.name, mode=cast(Literal["auto", "light", "dark"], mode)),
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
        effective_reading = effective.reading or RuntimeTuiReadingPreferences(sidebar_collapsed=False)
        self._tui_preferences = RuntimeTuiPreferences(
            theme=prefs.theme,
            reading=RuntimeTuiReadingPreferences(
                wrap=reading_prefs.wrap,
                sidebar_collapsed=not (effective_reading.sidebar_collapsed if effective_reading.sidebar_collapsed is not None else False),
            ),
        )
        self._apply_tui_preferences()
        self._persist_global_preferences()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "composer-input":
            return
        prompt = event.value.strip()
        if not prompt:
            return

        if prompt.startswith("/"):
            event.input.value = ""
            self._handle_slash_command(prompt)
            return

        event.input.value = ""
        if self._stream_active or self.pending_request_id is not None or self.pending_question_request_id is not None:
            if self.session_id is None:
                event.input.value = prompt
                self.notify("Cannot queue steering without an active session", severity="error")
                return
            try:
                queued = self.runtime.queue_steering(self.session_id, prompt)
            except Exception:
                event.input.value = prompt
                logger.exception("Failed to persist TUI steering message")
                self.notify("Runtime rejected steering message", severity="error")
                return
            self.query_one("#transcript-log", TimelineView).write(Text(f"Steering queued ({len(queued)}): {prompt}", style="dim cyan"))
            self.notify(f"Steering queued · {len(queued)} waiting")
            return

        self._start_prompt(prompt)

    def _start_prompt(self, prompt: str) -> None:
        self._begin_stream_render()
        self._write_user_prompt(prompt)
        self._set_state("Running")
        self._set_stream_active(True)
        self._current_prompt = prompt
        self._streamed_provider_text = False
        self._stream_output_buffer = ""
        self._thinking_buffer = ""

        request = RuntimeRequest(
            prompt=prompt,
            session_id=self.session_id,
            allocate_session_id=self.session_id is None,
            metadata={"provider_stream": True},
        )
        self._start_stream(request)

    def _begin_stream_render(self) -> None:
        self._stream_render_counter += 1
        prefix = f"turn-{self._stream_render_counter}"
        self._active_thinking_key = f"{prefix}-thinking"
        self._active_response_key = f"{prefix}-response"

    def _ensure_stream_render(self) -> tuple[str, str]:
        if self._active_thinking_key is None or self._active_response_key is None:
            self._begin_stream_render()
        assert self._active_thinking_key is not None
        assert self._active_response_key is not None
        return self._active_thinking_key, self._active_response_key

    def _handle_slash_command(self, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command == "/expand":
            self._handle_expand(arg)
            return
        self.query_one("#transcript-log", TimelineView).write(Text(f"Unknown command: {command}", style="bold red"))

    def _handle_expand(self, tool_call_id: str) -> None:
        log = self.query_one("#transcript-log", TimelineView)
        if not tool_call_id:
            log.write(Text("Usage: /expand <tool_call_id>", style="bold yellow"))
            return

        content: str | None = None
        artifact_id = self._tool_artifact_by_call_id.get(tool_call_id)
        if artifact_id is not None and self.session_id is not None:
            try:
                result = self.runtime.read_tool_output_artifact(
                    session_id=self.session_id,
                    tool_call_id=tool_call_id,
                    limit=10000,
                )
            except Exception as exc:
                logger.error("Failed to read tool output artifact: %s", exc)
                result = None
            if isinstance(result, dict):
                status = result.get("status")
                candidate = result.get("content")
                if status == "available" and isinstance(candidate, str):
                    content = candidate

        if content is None:
            content = self._tool_content_by_call_id.get(tool_call_id)

        if content is None:
            log.write(Text(f"✖ No stored output for tool_call_id: {tool_call_id}", style="bold red"))
            return

        if log.has_block(tool_call_id):
            display = self._tool_display_by_call_id.get(tool_call_id)
            path = self._display_copyable_path(display)
            kind = self._display_field(display, "kind") or ""
            renderable: RenderableType = Text(content)
            if kind == "edit":
                renderable = self._diff_renderable(content)
            elif kind in ("read", "search"):
                renderable = self._build_syntax_for_path(path, content) or Text(content)
            log.update_block(tool_call_id, content=renderable)
            log.expand_block(tool_call_id)
            return

        log.write(Text(f"── /expand {tool_call_id} ──", style="bold cyan"))
        display = self._tool_display_by_call_id.get(tool_call_id)
        path = self._display_copyable_path(display)
        kind = self._display_field(display, "kind") or ""
        if kind == "search":
            syntax = self._build_syntax_for_path(path, content)
            if syntax is not None:
                log.write(syntax)
                return
        log.write(Text(content))

    def _write_user_prompt(self, prompt: str) -> None:
        self.query_one("#transcript-log", TimelineView).write(
            Text(prompt),
            classes="timeline-entry user-message",
        )

    def _write_question_answers(self, request_id: str, responses: tuple[QuestionResponse, ...]) -> None:
        content = Text()
        for index, response in enumerate(responses):
            content.append(f"{response.header}\n", style="bold cyan")
            content.append("; ".join(response.answers), style="green")
            if index < len(responses) - 1:
                content.append("\n\n")
        self.query_one("#transcript-log", TimelineView).write_block(
            "✓ Answered",
            content,
            key=f"question-answer-{request_id}",
            collapsed=False,
            classes="timeline-block question-answer tool-success",
        )

    def _write_event_line(self, event: EventEnvelope) -> None:
        if event.event_type == "graph.provider_stream":
            payload = event.payload or {}
            if payload.get("channel") == "reasoning" and payload.get("kind") in {"delta", "content"}:
                text = payload.get("text")
                if isinstance(text, str) and text:
                    self._thinking_buffer += text
                    thinking_key, _ = self._ensure_stream_render()
                    log = self.query_one("#transcript-log", TimelineView)
                    thinking = Text(self._thinking_buffer, style="dim italic")
                    if log.has_block(thinking_key):
                        log.update_block(thinking_key, content=thinking)
                    else:
                        log.write_block(
                            "◐ Thinking",
                            thinking,
                            key=thinking_key,
                            collapsed=False,
                            classes="timeline-block thinking-block",
                        )
                return
            if payload.get("channel") == "text" and payload.get("kind") in {"delta", "content"}:
                text = payload.get("text")
                if isinstance(text, str) and text:
                    self._streamed_provider_text = True
                    thinking_key, _ = self._ensure_stream_render()
                    self.query_one("#transcript-log", TimelineView).collapse_block(thinking_key)
                    self._pending_output.append(text)
                    self._schedule_stream_preview_flush()
            return
        if event.event_type not in (
            "graph.tool_request_created",
            "runtime.tool_started",
            "runtime.tool_progress",
            "runtime.tool_completed",
            "runtime.approval_requested",
            "runtime.failed",
            "runtime.approval_resolved",
            "runtime.question_requested",
            "runtime.question_answered",
            "runtime.background_task_idle_reminder",
            "runtime.background_task_completed",
            "runtime.background_task_failed",
            "runtime.background_task_cancelled",
            "runtime.background_task_waiting_approval",
            "runtime.background_task_group_completed",
            "runtime.delegated_result_available",
        ):
            return

        payload = event.payload or {}
        if event.event_type.startswith("runtime.background_task_") or event.event_type == "runtime.delegated_result_available":
            self._render_background_event(event.event_type, payload, sequence=event.sequence)
            return
        raw_tool_name = payload.get("tool", "unknown_tool")
        tool_name = raw_tool_name if isinstance(raw_tool_name, str) else "unknown_tool"
        tool_call_id = payload.get("tool_call_id")
        display = self._extract_display(payload)

        if event.event_type == "graph.tool_request_created":
            text = self._render_tool_request_line(tool_name, display)
            log = self.query_one("#transcript-log", TimelineView)
            if isinstance(tool_call_id, str) and tool_call_id:
                if display is not None:
                    self._tool_display_by_call_id[tool_call_id] = display
                log.write_block(
                    text.plain,
                    self._tool_call_details(display),
                    key=tool_call_id,
                    classes="timeline-block tool-pending",
                )
            else:
                log.write(text)
            return

        if event.event_type == "runtime.tool_started":
            if isinstance(tool_call_id, str) and display is not None:
                self._tool_display_by_call_id[tool_call_id] = display
            log = self.query_one("#transcript-log", TimelineView)
            title = self._tool_lifecycle_title("◐", tool_name, display)
            if isinstance(tool_call_id, str) and tool_call_id:
                if log.has_block(tool_call_id):
                    log.update_block(tool_call_id, title=title, classes="timeline-block tool-running")
                else:
                    log.write_block(title, key=tool_call_id, classes="timeline-block tool-running")
            else:
                log.write(Text(title, style="dim"))
            return

        if event.event_type == "runtime.tool_progress":
            self._buffer_tool_progress(payload)
            if isinstance(tool_call_id, str):
                self._update_tool_progress_block(tool_call_id, tool_name, display)
            return

        if event.event_type == "runtime.tool_completed":
            self._render_tool_completed(tool_name, payload, display)
            return

        if event.event_type == "runtime.approval_requested":
            request_id = payload.get("request_id")
            if isinstance(request_id, str) and request_id:
                self._approval_context_by_request_id[request_id] = payload
            text = Text(f"⚠ Approval requested for tool: {tool_name}", style="bold yellow")
        elif event.event_type == "runtime.approval_resolved":
            decision = payload.get("decision", "unknown")
            request_id = payload.get("request_id")
            context = self._approval_context_by_request_id.pop(request_id, None) if isinstance(request_id, str) else None
            resolved_tool = context.get("tool") if context is not None else None
            if isinstance(resolved_tool, str) and resolved_tool:
                text = Text(f"ℹ Approval {decision} for tool: {resolved_tool}", style="bold cyan")
            else:
                text = Text(f"ℹ Approval {decision}", style="bold cyan")
        elif event.event_type == "runtime.question_requested":
            count = payload.get("question_count", 1)
            text = Text(f"? Agent requested input ({count})", style="bold yellow")
        elif event.event_type == "runtime.question_answered":
            text = Text("ℹ Answer submitted", style="bold cyan")
        elif event.event_type == "runtime.failed":
            error_msg = payload.get("error_summary", payload.get("error", "Unknown error"))
            formatted_error = self._format_runtime_error(error_msg)
            text = Text(f"✖ Failed: {formatted_error}", style="bold red")
        else:
            text = Text(f"EVENT {event.event_type} source={event.source}", style="dim")

        self.query_one("#transcript-log", TimelineView).write(text)

    def _render_background_event(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        sequence: int,
    ) -> None:
        log = self.query_one("#transcript-log", TimelineView)
        task_id = payload.get("task_id")
        task_label = task_id if isinstance(task_id, str) and task_id else "background task"
        short_task_id = task_label.removeprefix("task-")[:12]
        summary = payload.get("summary_output")
        error = payload.get("error")
        child_session_id = payload.get("child_session_id")

        if event_type == "runtime.background_task_completed":
            title = f"✓ Background completed · {short_task_id}"
            style = "bold green"
            body = summary if isinstance(summary, str) and summary else "Result is available through background_output."
        elif event_type == "runtime.background_task_failed":
            title = f"✖ Background failed · {short_task_id}"
            style = "bold red"
            body = error if isinstance(error, str) and error else "Background task failed."
        elif event_type == "runtime.background_task_cancelled":
            title = f"■ Background cancelled · {short_task_id}"
            style = "bold yellow"
            body = error if isinstance(error, str) and error else "Background task was cancelled."
        elif event_type == "runtime.background_task_waiting_approval":
            title = f"⚠ Background waiting for approval · {short_task_id}"
            style = "bold yellow"
            body = "Open the child session to resolve its pending approval."
        elif event_type == "runtime.background_task_idle_reminder":
            title = f"◌ Background waiting · {short_task_id}"
            style = "bold yellow"
            reminder = payload.get("reminder")
            body = reminder if isinstance(reminder, str) and reminder else "Delegated child session is waiting for external action."
        elif event_type == "runtime.background_task_group_completed":
            group_id = payload.get("parallel_group_id")
            title = f"✓ Background group completed · {group_id or 'group'}"
            style = "bold green"
            body = f"Terminal tasks: {payload.get('terminal_task_count', 0)}"
        else:
            title = f"↳ Delegated result available · {short_task_id}"
            style = "bold cyan"
            body = summary if isinstance(summary, str) and summary else "Delegated result is ready to read."

        if event_type in {
            "runtime.background_task_completed",
            "runtime.background_task_failed",
            "runtime.background_task_cancelled",
        } and isinstance(task_id, str):
            self._tracked_background_task_ids.discard(task_id)

        details = Text(body)
        if isinstance(child_session_id, str) and child_session_id:
            details.append(f"\nchild: {child_session_id}", style="dim")
        details.append(f"\ntask: {task_label}", style="dim")
        log.write_block(
            title,
            details,
            key=f"background-event-{sequence}-{task_label}",
            collapsed=False,
            classes="timeline-block background-event",
        )
        self.notify(Text(title, style=style).plain)

    def _poll_parent_session_events(self) -> None:
        self._background_poll_timer_scheduled = False
        if self._stream_active or self._background_event_poll_active or self.session_id is None or not self._tracked_background_task_ids:
            if self._stream_active and self._tracked_background_task_ids:
                self._schedule_background_event_poll()
            return
        self._background_event_poll_active = True
        self._poll_parent_session_events_worker(self.session_id)

    def _schedule_background_event_poll(self) -> None:
        if self._background_poll_timer_scheduled or not self._tracked_background_task_ids:
            return
        self._background_poll_timer_scheduled = True
        self.set_timer(1.0, self._poll_parent_session_events)

    @work(thread=True)
    def _poll_parent_session_events_worker(self, session_id: str) -> None:
        try:
            last_sequence = self._last_event_sequence_by_session.get(session_id, 0)
            replayed_chunks = tuple(self.runtime.resume_stream(session_id=session_id))
            new_chunks = self._ordered_new_event_chunks(replayed_chunks, after_sequence=last_sequence)
            for chunk in new_chunks:
                event = chunk.event
                assert event is not None
                if event.event_type in {
                    "runtime.background_task_completed",
                    "runtime.background_task_failed",
                    "runtime.background_task_cancelled",
                }:
                    task_id = event.payload.get("task_id")
                    if isinstance(task_id, str):
                        self._tracked_background_task_ids.discard(task_id)
            if new_chunks:
                self.post_message(ParentSessionEventsPolled(session_id, tuple(new_chunks)))
        except Exception as exc:
            logger.debug("Failed to poll parent session events: %s", exc)
        finally:
            self._background_event_poll_active = False
            if self._tracked_background_task_ids:
                self.call_from_thread(self._schedule_background_event_poll)

    @staticmethod
    def _ordered_new_event_chunks(
        chunks: tuple[RuntimeStreamChunk, ...],
        *,
        after_sequence: int,
    ) -> list[RuntimeStreamChunk]:
        new_chunks = [chunk for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.sequence > after_sequence]
        new_chunks.sort(key=lambda chunk: chunk.event.sequence if chunk.event is not None else 0)
        return new_chunks

    @staticmethod
    def _extract_display(payload: dict[str, object]) -> dict[str, object] | None:
        raw = payload.get("display")
        if isinstance(raw, dict):
            return cast(dict[str, object], raw)
        return None

    @staticmethod
    def _display_field(display: dict[str, object] | None, key: str) -> str | None:
        if display is None:
            return None
        value = display.get(key)
        return value if isinstance(value, str) and value else None

    @classmethod
    def _display_copyable_path(cls, display: dict[str, object] | None) -> str | None:
        if display is None:
            return None
        copyable = display.get("copyable")
        if not isinstance(copyable, dict):
            return None
        path = cast(dict[str, object], copyable).get("path")
        return path if isinstance(path, str) and path else None

    def _render_tool_request_line(self, tool_name: str, display: dict[str, object] | None) -> Text:
        title = self._display_field(display, "title")
        summary = self._display_field(display, "summary")
        if title and summary:
            text = Text(f"▶ {title}: {summary}", style="bold blue")
        elif summary:
            text = Text(f"▶ {summary}", style="bold blue")
        else:
            text = Text(f"▶ Started tool: {tool_name}", style="bold blue")
        path = self._display_copyable_path(display)
        if path:
            text.append(f"\n  {path}", style="dim")
        command = self._display_command(display)
        if command:
            text.append(f"\n  $ {command}", style="cyan")
        return text

    @classmethod
    def _tool_call_details(cls, display: dict[str, object] | None) -> Text:
        if display is None:
            return Text("")
        lines: list[str] = []
        args = display.get("args")
        if isinstance(args, list):
            lines.extend(str(arg) for arg in args if isinstance(arg, (str, int, float, bool)))
        command = cls._display_command(display)
        if command and command not in lines:
            lines.insert(0, f"$ {command}")
        path = cls._display_copyable_path(display)
        if path and path not in lines:
            lines.insert(0, path)
        return Text("\n".join(lines), style="dim")

    @classmethod
    def _display_command(cls, display: dict[str, object] | None) -> str | None:
        if display is None:
            return None
        copyable = display.get("copyable")
        if not isinstance(copyable, dict):
            return None
        command = cast(dict[str, object], copyable).get("command")
        return command if isinstance(command, str) and command else None

    def _buffer_tool_progress(self, payload: dict[str, object]) -> None:
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        chunk = payload.get("chunk")
        if not isinstance(chunk, str) or not chunk:
            return
        stream = payload.get("stream")
        stream_name = stream if isinstance(stream, str) and stream else "stdout"
        streams = self._pending_tool_progress.setdefault(tool_call_id, {})
        buffer = streams.setdefault(stream_name, [])
        if buffer:
            buffer[-1] = buffer[-1] + chunk
        else:
            buffer.append(chunk)

    def _update_tool_progress_block(
        self,
        tool_call_id: str,
        tool_name: str,
        display: dict[str, object] | None,
    ) -> None:
        log = self.query_one("#transcript-log", TimelineView)
        streams = self._pending_tool_progress.get(tool_call_id)
        if not streams:
            return
        content = self._tool_progress_renderable(streams)
        title = self._tool_lifecycle_title("◐", tool_name, display)
        if log.has_block(tool_call_id):
            log.update_block(tool_call_id, title=title, content=content, classes="timeline-block tool-running")
        else:
            log.write_block(title, content, key=tool_call_id, classes="timeline-block tool-running")

    @staticmethod
    def _tool_progress_renderable(streams: dict[str, list[str]]) -> Group:
        entries: list[RenderableType] = []
        for stream_name, chunks in streams.items():
            body = "".join(chunks).rstrip()
            if not body:
                continue
            style = "dim" if stream_name == "stdout" else "dim red"
            entries.append(Text(f"└ {stream_name}", style=style))
            entries.append(Text(body, style=style))
        return Group(*entries)

    def _tool_lifecycle_title(
        self,
        icon: str,
        tool_name: str,
        display: dict[str, object] | None,
    ) -> str:
        title = self._display_field(display, "title")
        summary = self._display_field(display, "summary")
        if title and summary:
            return f"{icon} {title}: {summary}"
        if summary:
            return f"{icon} {summary}"
        return f"{icon} {tool_name}"

    def _flush_tool_progress(self, tool_call_id: str | None) -> None:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        streams = self._pending_tool_progress.pop(tool_call_id, None)
        if not streams:
            return
        log = self.query_one("#transcript-log", TimelineView)
        for stream_name, chunks in streams.items():
            body = "".join(chunks).rstrip()
            if not body:
                continue
            style = "dim" if stream_name == "stdout" else "dim red"
            log.write(Text(f"  ⎿ {stream_name}:", style=style))
            log.write(Text(body, style=style))

    def _render_tool_completed(
        self,
        tool_name: str,
        payload: dict[str, object],
        display: dict[str, object] | None,
    ) -> None:
        log = self.query_one("#transcript-log", TimelineView)
        tool_call_id = payload.get("tool_call_id")
        tool_call_id_str = tool_call_id if isinstance(tool_call_id, str) else None
        if tool_name == "task" and payload.get("status") == "ok":
            background_task_id = payload.get("task_id")
            background_status = payload.get("status")
            if isinstance(background_task_id, str) and background_task_id and background_status in {"queued", "running", "ok"}:
                self._tracked_background_task_ids.add(background_task_id)
                self._schedule_background_event_poll()

        if display is None and tool_call_id_str is not None:
            display = self._tool_display_by_call_id.get(tool_call_id_str)

        status = payload.get("status")
        is_error = status == "error"
        header_style = "bold red" if is_error else "bold green"
        icon = "✖" if is_error else "✔"

        header = self._tool_lifecycle_title(icon, tool_name, display)
        body: list[RenderableType] = []
        streams = self._pending_tool_progress.pop(tool_call_id_str, None) if tool_call_id_str is not None else None
        if streams:
            body.append(self._tool_progress_renderable(streams))

        kind = self._display_field(display, "kind") or ""
        content = payload.get("content")
        if isinstance(content, str) and content and tool_call_id_str is not None:
            self._tool_content_by_call_id[tool_call_id_str] = content
            artifact = payload.get("artifact_id")
            if isinstance(artifact, str) and artifact:
                self._tool_artifact_by_call_id[tool_call_id_str] = artifact

        if isinstance(content, str) and content and kind != "write":
            path = self._display_copyable_path(display)
            if kind == "edit":
                body.append(self._diff_renderable(content))
            elif kind in ("read", "search"):
                body.append(self._build_syntax_for_path(path, content) or self._content_preview(content, tool_call_id_str))
            else:
                body.append(self._content_preview(content, tool_call_id_str))

        rendered_body: RenderableType = Group(*body)
        if tool_call_id_str is not None:
            if log.has_block(tool_call_id_str):
                log.update_block(
                    tool_call_id_str,
                    title=header,
                    content=rendered_body,
                    classes=f"timeline-block tool-{'error' if is_error else 'success'}",
                )
            else:
                log.write_block(header, rendered_body, key=tool_call_id_str, classes=f"timeline-block tool-{'error' if is_error else 'success'}")
        else:
            log.write(Text(header, style=header_style))
            for renderable in body:
                log.write(renderable)

    @staticmethod
    def _build_syntax_for_path(path: str | None, content: str) -> Syntax | None:
        if not path:
            return None
        try:
            lexer = Syntax.guess_lexer(path, code=content)
        except Exception:
            return None
        try:
            return Syntax(content, lexer, line_numbers=True, theme="monokai")
        except Exception:
            return None

    @staticmethod
    def _diff_renderable(content: str) -> Text:
        lines = content.splitlines()
        if not any(line.startswith(("+++", "---", "@@")) for line in lines):
            return Text(content)
        text = Text()
        for line in lines:
            if line.startswith("+++"):
                text.append(line + "\n", style="bold green")
            elif line.startswith("---"):
                text.append(line + "\n", style="bold red")
            elif line.startswith("+"):
                text.append(line + "\n", style="green")
            elif line.startswith("-"):
                text.append(line + "\n", style="red")
            elif line.startswith("@@"):
                text.append(line + "\n", style="blue")
            else:
                text.append(line + "\n")
        return text

    @staticmethod
    def _content_preview(
        content: str,
        tool_call_id: str | None,
        *,
        max_lines: int = 10,
        preview_head_lines: int = 5,
    ) -> Text:
        lines = content.splitlines()
        if len(lines) <= max_lines:
            return Text(content)
        head = "\n".join(lines[:preview_head_lines])
        remaining = len(lines) - preview_head_lines
        hint = f"\n… {remaining} more lines"
        if tool_call_id:
            hint += f" · Ctrl+O or /expand {tool_call_id}"
        text = Text(head)
        text.append(hint, style="dim")
        return text

    @staticmethod
    def _write_content_block(
        log: TimelineView,
        content: str,
        tool_call_id: str | None,
        *,
        max_lines: int = 10,
        preview_head_lines: int = 5,
    ) -> None:
        lines = content.splitlines()
        if len(lines) <= max_lines:
            log.write(Text(content))
            return
        head = "\n".join(lines[:preview_head_lines])
        log.write(Text(head))
        remaining = len(lines) - preview_head_lines
        hint = f"[... {remaining} more lines"
        if tool_call_id:
            hint += f", /expand {tool_call_id}"
        hint += "]"
        log.write(Text(hint, style="dim"))

    def _write_output_line(self, output: str) -> None:
        self.query_one("#transcript-log", TimelineView).write(Markdown(output))

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
    def _context_str_value(context_window: dict[str, object], key: str, default: str = "unknown") -> str:
        value = context_window.get(key, default)
        return value if isinstance(value, str) else default

    def _context_panel_label(self, metadata: dict[str, object] | None) -> str:
        if not metadata or "context_window" not in metadata:
            return "Unknown"

        cw = metadata["context_window"]
        if not isinstance(cw, dict):
            return "Unknown"
        context_window = cast(dict[str, object], cw)

        retained = self._context_int_value(context_window, "retained_tool_result_count")
        token_budget = self._context_int_value(context_window, "token_budget")

        text = f"{retained} results"
        if token_budget > 0:
            text += f"\n[Budget: {token_budget} tokens]"

        if self._context_int_value(context_window, "compacted", 0) or context_window.get("compacted") is True:
            reason = self._context_str_value(context_window, "compaction_reason")
            text += f"\n[Compacted: {reason}]"

        return text

    def _update_context_panel(self, metadata: dict[str, object] | None) -> None:
        self.query_one("#context-panel", Static).update(self._context_panel_label(metadata))

    @work(thread=True)
    def _update_context_panel_worker(self, metadata: dict[str, object] | None) -> None:
        label = self._context_panel_label(metadata)
        self.post_message(ContextPanelUpdated(label))

    def on_context_panel_updated(self, message: ContextPanelUpdated) -> None:
        # Async updates may arrive while a modal is the active screen. Sidebar
        # widgets belong to the root screen, so never resolve them through the
        # current-screen App.query_one() path.
        if not self.screen_stack:
            return
        self.screen_stack[0].query_one("#context-panel", Static).update(message.label)

    def _schedule_stream_preview_flush(self) -> None:
        if self._preview_flush_scheduled:
            return
        self._preview_flush_scheduled = True
        self.set_timer(0.1, self._flush_stream_preview)

    def _flush_stream_preview(self) -> None:
        self._preview_flush_scheduled = False
        if not self._pending_output:
            return
        output = "".join(self._pending_output)
        self._pending_output.clear()
        self._stream_output_buffer += output
        _, response_key = self._ensure_stream_render()
        log = self.query_one("#transcript-log", TimelineView)
        preview = Text(self._stream_output_buffer)
        if not log.update_live(response_key, preview):
            log.write_live(response_key, preview)

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
        elif state == "Waiting input":
            current.update("Waiting for your answer...")
        elif state == "Completed":
            current.update("")
        elif state == "Failed":
            current.update("Stream failed.")

    def _set_stream_active(self, active: bool) -> None:
        self._stream_active = active
        self.query_one("#composer-input", Input).disabled = False

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
    def _resume_stream(self, session_id: str, request_id: str, decision: Literal["allow", "deny"]) -> None:
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
    def _answer_question_stream(
        self,
        session_id: str,
        request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> None:
        last_status = "Idle"
        saw_chunk = False
        try:
            for chunk in self.runtime.answer_question_stream(
                session_id,
                question_request_id=request_id,
                responses=responses,
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
            self._update_context_panel_worker(chunk.session.metadata)

        if chunk.kind == "event" and chunk.event is not None:
            event_sequence = chunk.event.sequence
            if event_sequence > 0:
                last_sequence = self._last_event_sequence_by_session.get(self.session_id, 0)
                if event_sequence <= last_sequence:
                    return
                self._last_event_sequence_by_session[self.session_id] = event_sequence
            self._flush_stream_preview()
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

            if chunk.session.status == "waiting" and event.event_type == "runtime.approval_requested":
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

            if chunk.session.status == "waiting" and event.event_type == "runtime.question_requested":
                payload = event.payload or {}
                request_id = payload.get("request_id")
                self.pending_question_request_id = request_id if isinstance(request_id, str) and request_id else None
                self._set_state("Waiting input")
                self._set_stream_active(False)

                def _handle_answers(responses: tuple[QuestionResponse, ...] | None) -> None:
                    if responses is None or self.session_id is None or self.pending_question_request_id is None:
                        return
                    question_request_id = self.pending_question_request_id
                    self._write_question_answers(question_request_id, responses)
                    self.pending_question_request_id = None
                    self._set_state("Running")
                    self._set_stream_active(True)
                    self._answer_question_stream(self.session_id, question_request_id, responses)

                self.push_screen(QuestionModal(event), _handle_answers)
                return

            if event.event_type == "runtime.failed":
                self._set_state("Failed")
            elif chunk.session.status == "running":
                self._set_state("Running")
            elif chunk.session.status == "completed":
                self._set_state("Completed")

        elif chunk.kind == "output" and chunk.output is not None:
            # The graph emits the complete final answer after provider deltas.
            # Do not append it a second time when we already rendered deltas.
            if not self._streamed_provider_text:
                self._pending_output.append(chunk.output)
                self._schedule_stream_preview_flush()

    def on_parent_session_events_polled(self, message: ParentSessionEventsPolled) -> None:
        if self.session_id != message.session_id:
            return
        for chunk in message.chunks:
            self.on_stream_chunk_received(StreamChunkReceived(chunk))

    def on_stream_completed(self, message: StreamCompleted) -> None:
        self._flush_stream_preview()
        if message.final_status == "waiting":
            # The waiting chunk has already updated the underlying screen and
            # opened the approval modal. Do not query main-screen widgets while
            # that modal is the active screen.
            return
        if message.final_status == "failed":
            self._set_state("Failed")
        else:
            if self._stream_output_buffer and self._active_response_key is not None:
                log = self.query_one("#transcript-log", TimelineView)
                log.update_live(
                    self._active_response_key,
                    Markdown(self._stream_output_buffer),
                )
                log.finish_live(self._active_response_key)
            if self._thinking_buffer and self._active_thinking_key is not None:
                log = self.query_one("#transcript-log", TimelineView)
                log.update_block(
                    self._active_thinking_key,
                    title="Thinking",
                    content=Text(self._thinking_buffer, style="dim italic"),
                    classes="timeline-block thinking-block",
                )
                log.collapse_block(self._active_thinking_key)
            self._stream_output_buffer = ""
            self._thinking_buffer = ""
            self._active_thinking_key = None
            self._active_response_key = None
            self._set_state("Idle")
        self._set_stream_active(False)
        self.query_one("#composer-input", Input).focus()

    def on_stream_failed(self, message: StreamFailed) -> None:
        self._flush_stream_preview()
        self.query_one("#transcript-log", TimelineView).write(Text(f"Error: {self._format_runtime_error(message.error)}", style="bold red"))
        self.pending_request_id = None
        self.pending_question_request_id = None
        self._set_state("Failed")
        self._set_stream_active(False)
        self.query_one("#composer-input", Input).focus()
