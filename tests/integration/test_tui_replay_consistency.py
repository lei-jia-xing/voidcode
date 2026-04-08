from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tui.runtime_client import TuiRuntimeClient
from voidcode.tui.widgets.session_view import SessionView
from voidcode.tui.widgets.timeline import Timeline


class _SessionViewHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield SessionView(id="session-view")


@pytest.mark.anyio
async def test_tui_timeline_order_matches_between_live_stream_and_replay_order(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("alpha\nbeta\n", encoding="utf-8")
    client = TuiRuntimeClient(runtime=VoidCodeRuntime(workspace=tmp_path))

    live_chunks = tuple(client.stream_run("read sample.txt", session_id="tui-order-session"))
    replay_snapshot = client.open_session("tui-order-session")

    live_app = _SessionViewHarness()
    async with live_app.run_test() as pilot:
        live_view = live_app.query_one(SessionView)
        for chunk in live_chunks:
            live_view.apply_stream_chunk(chunk)
        await pilot.pause()
        live_events = live_view.display_state.timeline_events
        live_output = live_view.display_state.output_text
        live_rendered = [
            str(widget.render()) for widget in live_view.query_one(Timeline).query(Static).results()
        ]

    replay_app = _SessionViewHarness()
    async with replay_app.run_test() as pilot:
        replay_view = replay_app.query_one(SessionView)
        replay_view.show_snapshot(replay_snapshot)
        await pilot.pause()
        replay_events = replay_view.display_state.timeline_events
        replay_output = replay_view.display_state.output_text
        replay_rendered = [
            str(widget.render())
            for widget in replay_view.query_one(Timeline).query(Static).results()
        ]

    assert [ev.sequence for ev in live_events] == [ev.sequence for ev in replay_events]
    assert [ev.event_type for ev in live_events] == [ev.event_type for ev in replay_events]
    assert live_output == replay_output == "alpha\nbeta\n"
    assert any("Request received" in line for line in live_rendered)
    assert any("Tool requested" in line or "Tool resolved" in line for line in live_rendered)
    assert any("Request received" in line for line in replay_rendered)
