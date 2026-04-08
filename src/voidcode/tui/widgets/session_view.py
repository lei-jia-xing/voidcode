from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from ..models import TuiApprovalRequest, TuiSessionSnapshot, TuiStreamChunk, TuiTimelineEvent
from .prompt_bar import PromptBar
from .timeline import Timeline


@dataclass(frozen=True, slots=True)
class SessionDisplayState:
    session_id: str | None = None
    status: str = "idle"
    timeline_events: tuple[TuiTimelineEvent, ...] = ()
    timeline_empty_text: str = (
        "Start a prompt or open a persisted session with --session-id to replay runtime history."
    )
    output_text: str = ""
    approval_target: TuiApprovalRequest | None = None
    draft_text: str = ""


class ApprovalTargetPlaceholder(Static):
    def on_mount(self) -> None:
        self.can_focus = True


class SessionView(Widget):
    """Main pane showing the timeline and prompt bar."""

    class PromptSubmitted(Message):
        def __init__(self, prompt: str) -> None:
            super().__init__()
            self.prompt = prompt

    DEFAULT_CSS = """
    SessionView {
        width: 1fr;
        height: 100%;
        background: $background;
        padding: 1;
    }

    #session-header {
        padding-bottom: 1;
        color: $text;
        text-style: bold;
    }

    #session-output {
        min-height: 1;
        padding-top: 1;
        color: $text-muted;
    }

    #approval-target {
        margin-top: 1;
        padding: 1;
        border: solid $warning;
        background: $panel;
        color: $warning;
    }
    """

    def __init__(self, *, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._display_state = SessionDisplayState()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._header_text, id="session-header")
            yield Timeline(id="session-timeline")
            yield Static("", id="session-output")
            yield ApprovalTargetPlaceholder("", id="approval-target")
            yield PromptBar(id="session-prompt")

    def on_mount(self) -> None:
        self.can_focus = False
        self._refresh_view()

    @property
    def display_state(self) -> SessionDisplayState:
        return self._display_state

    @property
    def active_approval_target(self) -> TuiApprovalRequest | None:
        return self._display_state.approval_target

    @property
    def _header_text(self) -> str:
        if self._display_state.session_id is None:
            return "Session"
        return f"Session {self._display_state.session_id} · {self._display_state.status}"

    def show_snapshot(self, snapshot: TuiSessionSnapshot) -> None:
        self._display_state = SessionDisplayState(
            session_id=snapshot.session.session_id,
            status=snapshot.session.status,
            timeline_events=snapshot.timeline,
            timeline_empty_text="Persisted session loaded with no recorded runtime events.",
            output_text=snapshot.output or "",
            approval_target=snapshot.pending_approval,
            draft_text=self._display_state.draft_text,
        )
        self._refresh_view()

    def set_timeline_events(
        self,
        events: Iterable[TuiTimelineEvent],
        *,
        empty_message: str | None = None,
    ) -> None:
        self._display_state = SessionDisplayState(
            session_id=self._display_state.session_id,
            status=self._display_state.status,
            timeline_events=tuple(events),
            timeline_empty_text=empty_message or self._display_state.timeline_empty_text,
            output_text=self._display_state.output_text,
            approval_target=self._display_state.approval_target,
            draft_text=self._display_state.draft_text,
        )
        self._refresh_view()

    def apply_stream_chunk(self, chunk: TuiStreamChunk) -> None:
        approval_target = self._display_state.approval_target
        if chunk.approval_request is not None:
            approval_target = chunk.approval_request
        elif chunk.session.status != "waiting":
            approval_target = None

        events = self._display_state.timeline_events
        if chunk.event is not None:
            events = (*events, chunk.event)

        output_text = self._display_state.output_text
        if chunk.output is not None:
            output_text = chunk.output

        self._display_state = SessionDisplayState(
            session_id=chunk.session.session_id,
            status=chunk.session.status,
            timeline_events=events,
            timeline_empty_text=self._display_state.timeline_empty_text,
            output_text=output_text,
            approval_target=approval_target,
            draft_text=self.prompt_draft,
        )
        self._refresh_view()

    @property
    def prompt_draft(self) -> str:
        return self.query_one(PromptBar).draft

    @property
    def is_busy(self) -> bool:
        return self._display_state.status in {"running", "waiting"}

    def set_prompt_draft(self, draft_text: str) -> None:
        self._display_state = SessionDisplayState(
            session_id=self._display_state.session_id,
            status=self._display_state.status,
            timeline_events=self._display_state.timeline_events,
            timeline_empty_text=self._display_state.timeline_empty_text,
            output_text=self._display_state.output_text,
            approval_target=self._display_state.approval_target,
            draft_text=draft_text,
        )
        self._refresh_prompt_bar()

    def clear_prompt_draft(self) -> None:
        self.set_prompt_draft("")

    def can_submit_prompt(self) -> bool:
        return not self.is_busy

    def request_prompt_submit(self, prompt: str) -> bool:
        draft_text = prompt if prompt else self.prompt_draft
        self.set_prompt_draft(draft_text)
        if not prompt.strip() or not self.can_submit_prompt():
            return False
        self.post_message(self.PromptSubmitted(prompt=prompt.strip()))
        return True

    def begin_prompt_submission(self, prompt: str) -> None:
        self._display_state = SessionDisplayState(
            session_id=self._display_state.session_id,
            status="running",
            timeline_events=self._display_state.timeline_events,
            timeline_empty_text=self._display_state.timeline_empty_text,
            output_text=self._display_state.output_text,
            approval_target=None,
            draft_text=prompt,
        )
        self._refresh_view()

    def recover_from_submission_error(self) -> None:
        status = "waiting" if self._display_state.approval_target is not None else "idle"
        self._display_state = SessionDisplayState(
            session_id=self._display_state.session_id,
            status=status,
            timeline_events=self._display_state.timeline_events,
            timeline_empty_text=self._display_state.timeline_empty_text,
            output_text=self._display_state.output_text,
            approval_target=self._display_state.approval_target,
            draft_text=self._display_state.draft_text,
        )
        self._refresh_view()

    def on_prompt_bar_prompt_submit_requested(
        self, message: PromptBar.PromptSubmitRequested
    ) -> None:
        message.stop()
        self.request_prompt_submit(message.prompt)

    def focus_prompt(self) -> None:
        self.query_one("#prompt-input").focus()

    def focus_approval_target(self) -> None:
        self.query_one(ApprovalTargetPlaceholder).focus()

    def _refresh_view(self) -> None:
        self.query_one("#session-header", Static).update(self._header_text)
        self.query_one(Timeline).set_events(
            self._display_state.timeline_events,
            empty_message=self._display_state.timeline_empty_text,
        )
        self.query_one("#session-output", Static).update(self._display_state.output_text)
        self.query_one("#approval-target", Static).update(self._approval_target_text())
        self._refresh_prompt_bar()

    def _refresh_prompt_bar(self) -> None:
        prompt_bar = self.query_one(PromptBar)
        prompt_bar.set_draft(self._display_state.draft_text)
        prompt_bar.set_submit_disabled(self.is_busy)
        prompt_bar.set_status_text(self._prompt_status_text())

    def _prompt_status_text(self) -> str:
        if self._display_state.status == "running":
            return "Busy · run in progress"
        if self._display_state.status == "waiting":
            return "Waiting · approval required before continuing"
        return "Idle"

    def _approval_target_text(self) -> str:
        approval_target = self._display_state.approval_target
        if approval_target is None:
            return ""

        summary = approval_target.target_summary or approval_target.tool
        return (
            "Approval required: "
            f"{approval_target.request_id} · tool={approval_target.tool} · target={summary}"
        )
