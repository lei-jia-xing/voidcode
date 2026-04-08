from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from textual.widgets import TextArea

from voidcode.runtime.session import SessionStatus
from voidcode.tui.app import TuiBootstrap, VoidCodeTuiApp
from voidcode.tui.models import (
    TuiSessionSnapshot,
    TuiSessionState,
    TuiSessionSummary,
    TuiStreamChunk,
    TuiTimelineEvent,
)


@dataclass(slots=True)
class _StubRuntimeClient:
    list_sessions: MagicMock
    open_session: MagicMock
    stream_run: MagicMock
    resolve_approval: MagicMock


def _summary(
    session_id: str,
    *,
    status: SessionStatus = "completed",
    turn: int = 1,
    prompt: str = "read README.md",
    updated_at: int = 1700000000,
) -> TuiSessionSummary:
    return TuiSessionSummary(
        session_id=session_id,
        status=status,
        turn=turn,
        prompt=prompt,
        updated_at=updated_at,
    )


def _snapshot(
    session_id: str,
    *,
    status: SessionStatus = "completed",
    output: str | None = None,
    timeline: tuple[TuiTimelineEvent, ...] = (),
) -> TuiSessionSnapshot:
    return TuiSessionSnapshot(
        session=TuiSessionState(session_id=session_id, status=status, turn=2, metadata={}),
        timeline=timeline,
        output=output,
    )


def _approval_event(session_id: str) -> TuiTimelineEvent:
    return TuiTimelineEvent(
        session_id=session_id,
        sequence=5,
        event_type="runtime.approval_requested",
        source="runtime",
        payload={
            "request_id": "approval-1",
            "tool": "write_file",
            "target_summary": "write notes.txt",
            "reason": "write-capable tool invocation",
            "policy": {"mode": "ask"},
        },
    )


def _runtime_client(
    *,
    sessions: tuple[TuiSessionSummary, ...],
    snapshots: dict[str, TuiSessionSnapshot] | None = None,
) -> _StubRuntimeClient:
    snapshots = {} if snapshots is None else snapshots

    def _open_session(session_id: str) -> TuiSessionSnapshot:
        return snapshots[session_id]

    return _StubRuntimeClient(
        list_sessions=MagicMock(side_effect=lambda: sessions),
        open_session=MagicMock(side_effect=_open_session),
        stream_run=MagicMock(return_value=iter(cast(tuple[TuiStreamChunk, ...], ()))),
        resolve_approval=MagicMock(return_value=iter(cast(tuple[TuiStreamChunk, ...], ()))),
    )


@pytest.mark.anyio
async def test_boot_renders_empty_session_and_focuses_prompt() -> None:
    runtime_client = _runtime_client(sessions=())
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id=None),
        runtime_client=runtime_client,
    )

    async with app.run_test():
        session_view = app.get_active_session_view()

        assert app.active_session_id is None
        assert session_view.display_state.session_id is None
        assert isinstance(app.focused, TextArea)


@pytest.mark.anyio
async def test_direct_session_boot_opens_target_session() -> None:
    completed_snapshot = _snapshot(
        "session-2",
        status="completed",
        output="done\n",
        timeline=(
            TuiTimelineEvent(
                session_id="session-2",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "read README.md"},
            ),
        ),
    )
    runtime_client = _runtime_client(
        sessions=(
            _summary("session-1", status="failed", prompt="read docs"),
            _summary(
                "session-2", status="completed", prompt="read README.md", updated_at=1700000001
            ),
        ),
        snapshots={"session-2": completed_snapshot},
    )
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id="session-2"),
        runtime_client=runtime_client,
    )

    async with app.run_test():
        session_view = app.get_active_session_view()

        assert app.active_session_id == "session-2"
        assert session_view.display_state.session_id == "session-2"
        assert session_view.display_state.status == "completed"
        assert session_view.display_state.output_text == "done\n"
        assert session_view.active_approval_target is None
        assert isinstance(app.focused, TextArea)
        runtime_client.open_session.assert_called_once_with("session-2")


@pytest.mark.anyio
async def test_direct_session_boot_opens_waiting_session_and_focuses_approval_target() -> None:
    waiting_snapshot = _snapshot(
        "session-direct",
        status="waiting",
        timeline=(_approval_event("session-direct"),),
    )
    runtime_client = _runtime_client(
        sessions=(_summary("session-direct", status="waiting", prompt="write notes.txt"),),
        snapshots={"session-direct": waiting_snapshot},
    )
    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id="session-direct"),
        runtime_client=runtime_client,
    )

    async with app.run_test():
        session_view = app.get_active_session_view()

        assert app.active_session_id == "session-direct"
        assert session_view.active_approval_target is not None
        from textual.widgets import Button

        assert isinstance(app.focused, Button)
        assert app.focused.id == "btn-approve"
        runtime_client.open_session.assert_called_once_with("session-direct")
