from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.widgets import Button, Input

from voidcode.tui.app import TuiAppRuntimeClient, TuiBootstrap, VoidCodeTuiApp
from voidcode.tui.models import TuiSessionSnapshot, TuiSessionState, TuiTimelineEvent
from voidcode.tui.widgets.session_view import SessionView


@pytest.fixture
def mock_runtime_client() -> MagicMock:
    client = MagicMock(spec=TuiAppRuntimeClient)
    client.list_sessions.return_value = ()
    return client


def _waiting_snapshot(session_id: str = "session-1") -> TuiSessionSnapshot:
    return TuiSessionSnapshot(
        session=TuiSessionState(session_id=session_id, status="waiting", turn=1),
        timeline=(
            TuiTimelineEvent(
                session_id=session_id,
                sequence=1,
                event_type="runtime.approval_requested",
                source="runtime",
                payload={
                    "request_id": "req-123",
                    "tool": "shell_exec",
                    "target_summary": "ls",
                },
            ),
        ),
    )


@pytest.mark.anyio
async def test_app_handles_stream_run_error(mock_runtime_client: MagicMock):
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id=None),
        runtime_client=mock_runtime_client,
    )
    async with app.run_test() as pilot:
        app._handle_stream_run_error(ValueError("simulated error"))  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()

        session_view = app.query_one(SessionView)
        assert session_view.display_state.status == "idle"
        assert isinstance(app.focused, Input)


@pytest.mark.anyio
async def test_app_handles_stale_approval_error(mock_runtime_client: MagicMock):
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id=None),
        runtime_client=mock_runtime_client,
    )
    async with app.run_test() as pilot:
        app._handle_stale_approval_error(ValueError("stale approval"))  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()

        session_view = app.query_one(SessionView)
        assert session_view.display_state.status == "idle"
        assert isinstance(app.focused, Input)


@pytest.mark.anyio
async def test_app_waiting_session_modal_supports_keyboard_focus_navigation(
    mock_runtime_client: MagicMock,
):
    mock_runtime_client.open_session.return_value = _waiting_snapshot()
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id="session-1"),
        runtime_client=mock_runtime_client,
    )
    async with app.run_test() as pilot:
        assert isinstance(app.focused, Button)
        assert app.focused.id == "btn-approve"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert isinstance(app.focused, Button)
        assert app.focused.id == "btn-reject"
        await pilot.press("tab")
        await pilot.pause()
        assert isinstance(app.focused, Button)
        assert app.focused.id == "btn-approve"

        mock_runtime_client.resolve_approval.assert_not_called()
