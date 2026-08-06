from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Input, RichLog, Static

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
from ..runtime.service import VoidCodeRuntime
from ..runtime.session import StoredSessionSummary
from .messages import (
    ContextPanelUpdated,
    StreamChunkReceived,
    StreamCompleted,
    StreamFailed,
)
from .screens import (
    ApprovalModal,
    SessionListModal,
    ThemeModePickerModal,
    ThemePickerModal,
)

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

    def resume_stream(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> Iterator[RuntimeStreamChunk]: ...

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]: ...

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
        self._pending_output: list[str] = []
        self._stream_output_buffer = ""
        self._streamed_provider_text = False
        self._preview_flush_scheduled = False

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
                transcript_log = RichLog(id="transcript-log", markup=True, wrap=True, max_lines=2000)
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

    def _handle_command(self, command: str | None) -> None:
        if command == "session.new":
            self.session_id = None
            self._current_prompt = None
            self._set_state("Idle")
            self.query_one("#session-panel", Static).update("None")
            self._update_context_panel(None)
            self.query_one("#transcript-log", RichLog).clear()
            self.query_one("#transcript-log", RichLog).write(Text("--- New Session ---", style="bold"))
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
                    self.session_id = session_id

                    short_id = session_id.removeprefix("session-")[:8]
                    title = short_id
                    if session_id in self._session_titles:
                        title += f" - {self._session_titles[session_id][:30]}"
                    self.query_one("#session-panel", Static).update(title)

                    self.query_one("#transcript-log", RichLog).clear()
                    self.query_one("#transcript-log", RichLog).write(Text(f"--- Resumed Session {short_id} ---", style="bold"))
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
        self.query_one("#transcript-log", RichLog).wrap = wrap

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
        prompt = event.value.strip()
        if not prompt:
            return

        if prompt.startswith("/"):
            event.input.value = ""
            self._handle_slash_command(prompt)
            return

        if self._stream_active or self.pending_request_id is not None:
            return

        event.input.value = ""
        self._write_user_prompt(prompt)
        self._set_state("Running")
        self._set_stream_active(True)
        self._current_prompt = prompt
        self._streamed_provider_text = False
        self._stream_output_buffer = ""

        request = RuntimeRequest(
            prompt=prompt,
            session_id=self.session_id,
            allocate_session_id=self.session_id is None,
        )
        self._start_stream(request)

    def _handle_slash_command(self, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command == "/expand":
            self._handle_expand(arg)
            return
        self.query_one("#transcript-log", RichLog).write(Text(f"Unknown command: {command}", style="bold red"))

    def _handle_expand(self, tool_call_id: str) -> None:
        log = self.query_one("#transcript-log", RichLog)
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
        self.query_one("#transcript-log", RichLog).write(Text(f"User: {prompt}"))

    def _write_event_line(self, event: EventEnvelope) -> None:
        if event.event_type == "graph.provider_stream":
            payload = event.payload or {}
            if payload.get("channel") == "text" and payload.get("kind") in {"delta", "content"}:
                text = payload.get("text")
                if isinstance(text, str) and text:
                    self._streamed_provider_text = True
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
        ):
            return

        payload = event.payload or {}
        raw_tool_name = payload.get("tool", "unknown_tool")
        tool_name = raw_tool_name if isinstance(raw_tool_name, str) else "unknown_tool"
        tool_call_id = payload.get("tool_call_id")
        display = self._extract_display(payload)

        if event.event_type == "graph.tool_request_created":
            text = self._render_tool_request_line(tool_name, display)
            self.query_one("#transcript-log", RichLog).write(text)
            return

        if event.event_type == "runtime.tool_started":
            if isinstance(tool_call_id, str) and display is not None:
                self._tool_display_by_call_id[tool_call_id] = display
            text = Text(f"◉ {tool_name}: starting...", style="dim")
            self.query_one("#transcript-log", RichLog).write(text)
            return

        if event.event_type == "runtime.tool_progress":
            self._buffer_tool_progress(payload)
            return

        if event.event_type == "runtime.tool_completed":
            self._render_tool_completed(tool_name, payload, display)
            return

        if event.event_type == "runtime.approval_requested":
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
        return text

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

    def _flush_tool_progress(self, tool_call_id: str | None) -> None:
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        streams = self._pending_tool_progress.pop(tool_call_id, None)
        if not streams:
            return
        log = self.query_one("#transcript-log", RichLog)
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
        log = self.query_one("#transcript-log", RichLog)
        tool_call_id = payload.get("tool_call_id")
        tool_call_id_str = tool_call_id if isinstance(tool_call_id, str) else None

        self._flush_tool_progress(tool_call_id_str)

        if display is None and tool_call_id_str is not None:
            display = self._tool_display_by_call_id.get(tool_call_id_str)

        status = payload.get("status")
        is_error = status == "error"
        header_style = "bold red" if is_error else "bold green"
        icon = "✖" if is_error else "✔"

        title = self._display_field(display, "title")
        summary = self._display_field(display, "summary")
        if title and summary:
            header = Text(f"{icon} {title}: {summary}", style=header_style)
        elif summary:
            header = Text(f"{icon} {summary}", style=header_style)
        else:
            header = Text(f"{icon} Completed tool: {tool_name}", style=header_style)
        path = self._display_copyable_path(display)
        if path:
            header.append(f"\n  {path}", style="dim")
        log.write(header)

        kind = self._display_field(display, "kind") or ""
        if kind in ("write",):
            return

        content = payload.get("content")
        if not isinstance(content, str) or not content:
            return

        if tool_call_id_str is not None:
            self._tool_content_by_call_id[tool_call_id_str] = content
            artifact = payload.get("artifact_id")
            if isinstance(artifact, str) and artifact:
                self._tool_artifact_by_call_id[tool_call_id_str] = artifact

        if kind == "edit":
            self._write_diff_render(log, content)
            return

        if kind in ("read", "search"):
            syntax = self._build_syntax_for_path(path, content)
            if syntax is not None:
                log.write(syntax)
                return

        self._write_content_block(log, content, tool_call_id_str)

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
    def _write_diff_render(log: RichLog, content: str) -> None:
        lines = content.splitlines()
        if not any(line.startswith(("+++", "---", "@@")) for line in lines):
            log.write(Text(content))
            return
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
        log.write(text)

    @staticmethod
    def _write_content_block(
        log: RichLog,
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
        self.query_one("#context-panel", Static).update(message.label)

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
        self.query_one("#current-response", Static).update(Markdown(self._stream_output_buffer))

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
        self.query_one("#composer-input", Input).disabled = active or self.pending_request_id is not None

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

    def on_stream_chunk_received(self, message: StreamChunkReceived) -> None:
        chunk = message.chunk
        self.session_id = chunk.session.session.id

        if hasattr(chunk.session, "metadata") and chunk.session.metadata:
            self._update_context_panel_worker(chunk.session.metadata)

        if chunk.kind == "event" and chunk.event is not None:
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
            if self._stream_output_buffer:
                self._write_output_line(self._stream_output_buffer)
                self._stream_output_buffer = ""
            self._set_state("Idle")
        self._set_stream_active(False)

    def on_stream_failed(self, message: StreamFailed) -> None:
        self._flush_stream_preview()
        self.query_one("#transcript-log", RichLog).write(Text(f"Error: {self._format_runtime_error(message.error)}", style="bold red"))
        self.pending_request_id = None
        self._set_state("Failed")
        self._set_stream_active(False)
