from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.widgets import Input

from voidcode.tui.app import TuiBootstrap, VoidCodeTuiApp
from voidcode.tui.models import (
    TuiSessionSnapshot,
    TuiSessionState,
    TuiStreamChunk,
    TuiTimelineEvent,
)
from voidcode.tui.widgets.approval_modal import ApprovalModal


@pytest.fixture
def mock_runtime_client() -> MagicMock:
    client = MagicMock()
    # default setup
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
                payload={"request_id": "req-123", "tool": "shell_exec", "target_summary": "ls"},
            ),
        ),
    )


@pytest.mark.anyio
async def test_approval_decision_allow_resolves_and_resumes(mock_runtime_client: MagicMock):
    mock_runtime_client.open_session.return_value = _waiting_snapshot()
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id="session-1"),
        runtime_client=mock_runtime_client,
    )

    async with app.run_test() as pilot:
        modal = app.screen
        assert isinstance(modal, ApprovalModal)

        mock_runtime_client.resolve_approval.return_value = [  # pyright: ignore[reportUnknownMemberType]
            TuiStreamChunk(
                kind="event",
                session=TuiSessionState(session_id="session-1", status="completed", turn=1),
                event=TuiTimelineEvent(
                    session_id="session-1",
                    sequence=2,
                    event_type="runtime.approval_resolved",
                    source="runtime",
                    payload={"request_id": "req-123", "decision": "allow"},
                ),
            )
        ]

        await pilot.press("enter")
        await pilot.pause()
        for worker in app.workers:
            await worker.wait()
        await pilot.pause()

        mock_runtime_client.resolve_approval.assert_called_once_with(  # pyright: ignore[reportUnknownMemberType]
            session_id="session-1", request_id="req-123", decision="allow"
        )
        assert isinstance(app.focused, Input)


@pytest.mark.anyio
async def test_approval_decision_deny_resolves_and_resumes_with_keyboard_only(
    mock_runtime_client: MagicMock,
):
    mock_runtime_client.open_session.return_value = _waiting_snapshot()
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id="session-1"),
        runtime_client=mock_runtime_client,
    )

    async with app.run_test() as pilot:
        mock_runtime_client.resolve_approval.return_value = []  # pyright: ignore[reportUnknownMemberType]

        assert app.focused is not None
        assert app.focused.id == "btn-approve"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "btn-reject"
        await pilot.press("enter")
        await pilot.pause()
        for worker in app.workers:
            await worker.wait()

        mock_runtime_client.resolve_approval.assert_called_once_with(  # pyright: ignore[reportUnknownMemberType]
            session_id="session-1", request_id="req-123", decision="deny"
        )
