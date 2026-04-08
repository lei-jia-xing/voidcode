from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from .models import TuiApprovalRequest, TuiSessionSnapshot, TuiSessionSummary, TuiStreamChunk
from .runtime_client import TuiRuntimeClient
from .theme import DEVELOPER_THEME
from .widgets.approval_modal import ApprovalModal
from .widgets.prompt_bar import PromptBar
from .widgets.session_view import SessionView


class StartupView(Widget):
    class PromptSubmitted(Message):
        def __init__(self, prompt: str) -> None:
            super().__init__()
            self.prompt = prompt

    DEFAULT_CSS = """
    StartupView {
        width: 100%;
        height: 100%;
        align: center middle;
    }
    #startup-container {
        width: 60%;
        min-width: 40;
        max-width: 80;
        height: auto;
    }
    #startup-title {
        content-align: center middle;
        text-style: bold;
        color: $primary;
        margin-bottom: 2;
        width: 100%;
    }
    #startup-helper {
        content-align: center middle;
        color: $text-muted;
        margin-top: 1;
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="startup-container"):
            yield Static("VoidCode", id="startup-title")
            yield PromptBar(id="startup-prompt", minimal=True)
            yield Static("Enter submit · Ctrl+J newline · Ctrl+Q quit", id="startup-helper")

    def focus_prompt(self) -> None:
        self.query_one(PromptBar).query_one("Composer").focus()

    def on_prompt_bar_prompt_submit_requested(self, event: PromptBar.PromptSubmitRequested) -> None:
        event.stop()
        self.post_message(self.PromptSubmitted(event.prompt))


class StartupScreen(Screen[None]):
    def compose(self) -> ComposeResult:
        with Horizontal(id="main-container"):
            yield StartupView(id="startup-view")


class ConversationScreen(Screen[None]):
    def compose(self) -> ComposeResult:
        with Horizontal(id="main-container"):
            yield SessionView(id="session-view")


@dataclass(frozen=True)
class TuiBootstrap:
    workspace: Path
    session_id: str | None

    @property
    def startup_mode(self) -> str:
        if self.session_id is None:
            return "new_session"
        return "session"


class TuiAppRuntimeClient(Protocol):
    def list_sessions(self) -> tuple[TuiSessionSummary, ...]: ...

    def open_session(self, session_id: str) -> TuiSessionSnapshot: ...

    def stream_run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
        allocate_session_id: bool = False,
    ) -> Iterator[TuiStreamChunk]: ...

    def resolve_approval(
        self,
        *,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
    ) -> Iterator[TuiStreamChunk]: ...


class VoidCodeTuiApp(App[None]):
    TITLE = "VoidCode"

    BINDINGS = [
        Binding("tab", "focus_next", "Focus Next", show=False),
        Binding("shift+tab", "focus_previous", "Focus Prev", show=False),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        background: transparent;
    }
    #main-container {
        width: 100%;
        height: 100%;
    }
    """

    SCREENS = {
        "startup": StartupScreen,
        "conversation": ConversationScreen,
    }

    def __init__(
        self,
        bootstrap: TuiBootstrap,
        *,
        runtime_client: TuiAppRuntimeClient | None = None,
    ) -> None:
        super().__init__()
        self.bootstrap = bootstrap
        self.runtime_client = runtime_client or TuiRuntimeClient.for_workspace(
            workspace=self.bootstrap.workspace
        )
        self.active_session_id: str | None = None
        self._active_run_worker: Worker[tuple[TuiStreamChunk, ...]] | None = None

    def get_active_session_view(self) -> SessionView:
        for screen in reversed(self.screen_stack):
            try:
                return screen.query_one(SessionView)
            except Exception:
                pass
        return self.query_one(SessionView)

    def get_active_startup_view(self) -> StartupView:
        for screen in reversed(self.screen_stack):
            try:
                return screen.query_one(StartupView)
            except Exception:
                pass
        return self.query_one(StartupView)

    async def on_mount(self) -> None:
        self.register_theme(DEVELOPER_THEME)
        self.theme = "developer"

        if self.bootstrap.session_id is not None:
            await self.push_screen(ConversationScreen(id="conversation"))
            try:
                self._open_session(self.bootstrap.session_id)
                return
            except ValueError:
                await self.push_screen(StartupScreen(id="startup"))
                self.get_active_startup_view().focus_prompt()
                return

        await self.push_screen(StartupScreen(id="startup"))
        self.get_active_startup_view().focus_prompt()

    async def on_startup_view_prompt_submitted(self, message: StartupView.PromptSubmitted) -> None:
        prompt = message.prompt
        await self.push_screen(ConversationScreen(id="conversation"))
        session_view = self.get_active_session_view()
        session_view.set_prompt_draft(prompt)
        session_view.begin_prompt_submission(prompt)
        self._active_run_worker = self._stream_prompt_run(prompt)

    async def on_session_view_prompt_submitted(self, message: SessionView.PromptSubmitted) -> None:
        prompt = message.prompt
        session_view = self.get_active_session_view()
        session_view.begin_prompt_submission(prompt)
        self._active_run_worker = self._stream_prompt_run(prompt)

    @work(thread=True, exclusive=True)
    def _stream_prompt_run(self, prompt: str) -> tuple[TuiStreamChunk, ...]:
        session_id = self.active_session_id
        chunks: list[TuiStreamChunk] = []
        for chunk in self.runtime_client.stream_run(
            prompt,
            session_id=session_id,
            metadata={"client": "tui"},
            allocate_session_id=session_id is None,
        ):
            chunks.append(chunk)
            self.call_from_thread(self._apply_stream_chunk, chunk)
        return tuple(chunks)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = cast(Worker[tuple[TuiStreamChunk, ...]], event.worker)
        if self._active_run_worker is None or worker is not self._active_run_worker:
            return
        if event.state == WorkerState.ERROR:
            self._handle_stream_run_error(worker.error)
            self._active_run_worker = None
            return
        if event.state == WorkerState.SUCCESS:
            self._handle_stream_run_complete()
            self._active_run_worker = None

    def _apply_stream_chunk(self, chunk: TuiStreamChunk) -> None:
        self.active_session_id = chunk.session.session_id
        session_view = self.get_active_session_view()
        session_view.apply_stream_chunk(chunk)

    def _handle_stream_run_complete(self) -> None:
        session_view = self.get_active_session_view()
        session_view.clear_prompt_draft()
        if session_view.active_approval_target is not None:
            self._present_approval_modal(session_view.active_approval_target)
            return
        session_view.focus_prompt()

    def _handle_stream_run_error(self, error: BaseException | None) -> None:
        session_view = self.get_active_session_view()
        session_view.recover_from_submission_error()
        message = "Prompt run failed"
        if error is not None:
            message = f"Prompt run failed: {error}"
        self.notify(message, severity="error")
        session_view.focus_prompt()

    def _open_session(self, session_id: str) -> None:
        snapshot = self.runtime_client.open_session(session_id)
        self.active_session_id = snapshot.session.session_id

        session_view = self.get_active_session_view()
        session_view.show_snapshot(snapshot)
        if snapshot.pending_approval is not None:
            self._present_approval_modal(snapshot.pending_approval)
            return

        session_view.focus_prompt()

    def _present_approval_modal(self, request: TuiApprovalRequest) -> None:
        self.push_screen(ApprovalModal(request), self._handle_approval_decision)

    def _handle_approval_decision(self, decision: bool | None) -> None:
        if decision is None:
            session_view = self.get_active_session_view()
            session_view.focus_approval_target()
            return

        session_view = self.get_active_session_view()
        if self.active_session_id is None or session_view.active_approval_target is None:
            return

        request_id = session_view.active_approval_target.request_id
        resolution = "allow" if decision else "deny"
        session_view.begin_prompt_submission("")
        self._active_run_worker = self._stream_approval_run(request_id, resolution)

    @work(thread=True, exclusive=True)
    def _stream_approval_run(
        self, request_id: str, decision: Literal["allow", "deny"]
    ) -> tuple[TuiStreamChunk, ...]:
        session_id = self.active_session_id
        if session_id is None:
            return ()

        chunks: list[TuiStreamChunk] = []
        try:
            for chunk in self.runtime_client.resolve_approval(
                session_id=session_id,
                request_id=request_id,
                decision=decision,
            ):
                chunks.append(chunk)
                self.call_from_thread(self._apply_stream_chunk, chunk)
        except Exception as e:
            self.call_from_thread(self._handle_stale_approval_error, e)
            raise
        return tuple(chunks)

    def _handle_stale_approval_error(self, error: Exception) -> None:
        session_view = self.get_active_session_view()
        session_view.recover_from_submission_error()
        self.notify(f"Approval resolution failed: {error}", severity="error")
        session_view.focus_prompt()


def launch_tui(*, workspace: Path, session_id: str | None) -> None:
    bootstrap = TuiBootstrap(workspace=workspace, session_id=session_id)
    app = VoidCodeTuiApp(bootstrap)
    app.run()
