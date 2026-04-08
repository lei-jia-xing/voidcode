from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from voidcode.tui.models import (
    TuiSessionSnapshot,
    TuiSessionState,
    TuiTimelineEvent,
)
from voidcode.tui.widgets.prompt_bar import PromptBar
from voidcode.tui.widgets.session_view import ApprovalTargetPlaceholder, SessionView
from voidcode.tui.widgets.timeline import Timeline
from voidcode.tui.widgets.tool_activity import ToolActivityBlock


def _visible_timeline_lines(timeline: Timeline) -> list[str]:
    return [
        str(widget.render())
        for widget in timeline.query(Static).results()
        if widget.id != "timeline-content" or str(widget.render()).strip()
    ]


class SessionViewTestApp(App[None]):
    def compose(self) -> ComposeResult:
        yield SessionView()


class SessionViewPromptHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.submitted_prompts: list[str] = []

    def compose(self) -> ComposeResult:
        yield SessionView()

    def on_session_view_prompt_submitted(self, message: SessionView.PromptSubmitted) -> None:
        self.submitted_prompts.append(message.prompt)


@pytest.mark.anyio
async def test_session_view_renders_grouped_tool_blocks():
    app = SessionViewTestApp()
    async with app.run_test() as pilot:
        session_view = app.query_one(SessionView)
        events = [
            TuiTimelineEvent(
                session_id="s1",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
            ),
            TuiTimelineEvent(
                session_id="s1",
                sequence=2,
                event_type="graph.tool_request_created",
                source="graph",
                payload={"tool": "read_file"},
            ),
            TuiTimelineEvent(
                session_id="s1",
                sequence=3,
                event_type="runtime.tool_lookup_succeeded",
                source="runtime",
            ),
            TuiTimelineEvent(
                session_id="s1",
                sequence=4,
                event_type="runtime.tool_completed",
                source="runtime",
            ),
        ]

        session_view.set_timeline_events(events)
        await pilot.pause()

        timeline = session_view.query_one(Timeline)

        # ToolActivityBlock has a Static inside it, plus the top-level Static
        tool_blocks = list(timeline.query(ToolActivityBlock).results())

        # ToolActivityBlock has a Static inside it, plus the top-level Static
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "read_file"
        assert tool_blocks[0].status == "completed"
        visible_lines = _visible_timeline_lines(timeline)
        assert any("Request received" in line for line in visible_lines)


@pytest.mark.anyio
async def test_session_view_displays_approval_placeholder():
    app = SessionViewTestApp()
    async with app.run_test() as pilot:
        session_view = app.query_one(SessionView)
        snapshot = TuiSessionSnapshot(
            session=TuiSessionState(session_id="s1", status="waiting", turn=1),
            timeline=(
                TuiTimelineEvent(
                    session_id="s1",
                    sequence=1,
                    event_type="runtime.approval_requested",
                    source="runtime",
                    payload={
                        "request_id": "req1",
                        "tool": "write_file",
                        "target_summary": "main.py",
                    },
                ),
            ),
        )

        session_view.show_snapshot(snapshot)
        await pilot.pause()

        target_widget = session_view.query_one(ApprovalTargetPlaceholder)
        content = target_widget.render()
        assert "write_file" in str(content)
        assert "main.py" in str(content)
        assert "req1" in str(content)


@pytest.mark.anyio
async def test_session_view_renders_unknown_event_as_generic_row():
    app = SessionViewTestApp()
    async with app.run_test() as pilot:
        session_view = app.query_one(SessionView)
        session_view.set_timeline_events(
            (
                TuiTimelineEvent(
                    session_id="s1",
                    sequence=9,
                    event_type="runtime.future_added",
                    source="runtime",
                    payload={"detail": "kept generic"},
                ),
            )
        )
        await pilot.pause()

        timeline = session_view.query_one(Timeline)
        rendered = _visible_timeline_lines(timeline)

        assert any("runtime.future_added" in line for line in rendered)
        assert any("detail=kept generic" in line for line in rendered)


@pytest.mark.anyio
async def test_session_view_snapshot_renders_visible_timeline_rows():
    app = SessionViewTestApp()
    async with app.run_test() as pilot:
        session_view = app.query_one(SessionView)
        snapshot = TuiSessionSnapshot(
            session=TuiSessionState(session_id="s1", status="completed", turn=2),
            timeline=(
                TuiTimelineEvent(
                    session_id="s1",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": "read README.md"},
                ),
                TuiTimelineEvent(
                    session_id="s1",
                    sequence=2,
                    event_type="graph.response_ready",
                    source="graph",
                    payload={"result": "done"},
                ),
            ),
            output="done\n",
        )

        session_view.show_snapshot(snapshot)
        await pilot.pause()

        timeline = session_view.query_one(Timeline)
        rendered = _visible_timeline_lines(timeline)

        assert any("Request received" in line for line in rendered)
        assert any("Response ready" in line for line in rendered)


@pytest.mark.anyio
async def test_session_view_submits_prompt_and_keeps_draft_visible():
    app = SessionViewPromptHarness()
    async with app.run_test() as pilot:
        session_view = app.query_one(SessionView)

        submitted = session_view.request_prompt_submit("read README.md")
        await pilot.pause()

        assert submitted is True
        assert session_view.prompt_draft == "read README.md"
        assert app.submitted_prompts == ["read README.md"]


@pytest.mark.anyio
async def test_session_view_blocks_duplicate_submit_while_busy_and_preserves_draft():
    app = SessionViewPromptHarness()
    async with app.run_test() as pilot:
        session_view = app.query_one(SessionView)

        session_view.begin_prompt_submission("read README.md")
        await pilot.pause()

        submitted = session_view.request_prompt_submit("write notes.txt")
        await pilot.pause()

        prompt_bar = session_view.query_one(PromptBar)

        assert submitted is False
        assert session_view.prompt_draft == "write notes.txt"
        assert prompt_bar.submit_disabled is True
        assert prompt_bar.status_text == "Busy · run in progress"
        assert prompt_bar.query_one("#prompt-input").disabled is True
        assert app.submitted_prompts == []
