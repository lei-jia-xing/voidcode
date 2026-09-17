"""TUI rendering contract for live provider deltas and provider restarts.

The runtime streams provider deltas as live-only events that share the persisted
sequence cursor, and it announces ``discarded_streamed_output`` when a transient
retry or provider fallback restarts an attempt whose deltas the client already
rendered. These tests pin both halves of the contract: the live deltas must
render even though their sequence is not fresh, and the restarted attempt's text
must be the only assistant output left in the turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


@dataclass(frozen=True)
class _StubEvent:
    sequence: int
    event_type: str
    source: str
    payload: dict[str, object]


@dataclass(frozen=True)
class _StubSessionRef:
    id: str
    parent_id: str | None = None


@dataclass(frozen=True)
class _StubSession:
    session: _StubSessionRef
    status: str
    turn: int = 1
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class _StubChunk:
    kind: str
    session: _StubSession
    event: _StubEvent | None = None
    output: str | None = None


def _event(event_type: str, *, sequence: int, source: str = "graph", **payload: object) -> _StubEvent:
    return _StubEvent(sequence=sequence, event_type=event_type, source=source, payload=dict(payload))


def _chunk(
    *,
    status: str,
    event: _StubEvent | None = None,
    output: str | None = None,
    session_id: str = "demo-session",
) -> _StubChunk:
    return _StubChunk(
        kind="output" if output is not None else "event",
        session=_StubSession(session=_StubSessionRef(id=session_id), status=status),
        event=event,
        output=output,
    )


def _plain_text(app: Any) -> str:
    lines = app.query_one("#transcript-log").lines
    return "\n".join("".join(segment.text for segment in line) for line in lines)


@pytest.fixture
def app_class() -> Any:
    from voidcode.tui import StreamChunkReceived, StreamCompleted, VoidCodeTUI

    return VoidCodeTUI, StreamChunkReceived, StreamCompleted


@pytest.mark.anyio
async def test_tui_renders_live_deltas_that_share_the_persisted_cursor(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=MagicMock())
    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _chunk(
                    status="running",
                    event=_event("runtime.request_received", sequence=1, source="runtime", prompt="hi"),
                )
            )
        )
        # Live deltas carry the current persisted cursor (1), not a fresh identity.
        for text in ("Hello ", "world"):
            app.on_stream_chunk_received(
                StreamChunkReceived(
                    _chunk(
                        status="running",
                        event=_event("graph.provider_stream", sequence=1, kind="delta", channel="text", text=text),
                    )
                )
            )
        app._flush_stream_preview()
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        assert "Hello world" in _plain_text(app)


@pytest.mark.anyio
async def test_tui_discards_restarted_attempt_text(app_class: Any) -> None:
    VoidCodeTUI, StreamChunkReceived, StreamCompleted = app_class

    app = VoidCodeTUI(workspace=Path("."), runtime=MagicMock())
    async with app.run_test() as pilot:
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _chunk(
                    status="running",
                    event=_event("runtime.request_received", sequence=1, source="runtime", prompt="hi"),
                )
            )
        )
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _chunk(
                    status="running",
                    event=_event("graph.provider_stream", sequence=1, kind="delta", channel="text", text="first attempt"),
                )
            )
        )
        app._flush_stream_preview()
        # The runtime keeps the retry and announces the discarded projection.
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _chunk(
                    status="running",
                    event=_event(
                        "runtime.provider_transient_retry",
                        sequence=2,
                        source="runtime",
                        reason="transient_failure",
                        discarded_streamed_output=True,
                    ),
                )
            )
        )
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _chunk(
                    status="running",
                    event=_event("graph.provider_stream", sequence=2, kind="delta", channel="text", text="second attempt"),
                )
            )
        )
        app._flush_stream_preview()
        app.on_stream_chunk_received(
            StreamChunkReceived(
                _chunk(
                    status="completed",
                    event=_event("graph.response_ready", sequence=3, output_preview="second attempt"),
                )
            )
        )
        app.on_stream_completed(StreamCompleted("completed"))
        await pilot.pause()

        plain = _plain_text(app)
        assert "second attempt" in plain
        assert "first attempt" not in plain
