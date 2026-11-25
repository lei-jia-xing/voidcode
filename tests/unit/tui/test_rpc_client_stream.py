"""CoreRpcClient stream acks tolerate a connection closing between frames."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed

from voidcode.sdk.client import EventFrame, StreamEvent
from voidcode.tui.client import _END, CoreRpcClient


class _FakeSocket:
    def __init__(self, *, ack_error: Exception | None = None) -> None:
        self.ack_error = ack_error
        self.methods: list[str] = []

    def send(self, text: str) -> None:
        message = json.loads(text)
        method = message["method"]
        if method == "session.ack" and self.ack_error is not None:
            raise self.ack_error
        self.methods.append(method)


def _event() -> EventFrame:
    return EventFrame(
        session_id="s",
        event=StreamEvent(
            session_id="s",
            sequence=4,
            type="core.notice",
            run_id="run",
            revision_id="rev",
            source="core",
            actor="agent",
            payload={},
        ),
    )


def _client(socket: _FakeSocket) -> CoreRpcClient:
    client = CoreRpcClient("http://127.0.0.1:1")

    def call(method: str, params: Mapping[str, Any] | None = None, *, timeout: float = 300) -> dict[str, Any]:
        return {}

    client._call = call  # type: ignore[method-assign]
    client._socket = socket  # type: ignore[assignment]
    return client


async def _consume(client: CoreRpcClient) -> list[EventFrame]:
    frames: list[EventFrame] = []

    async def collect() -> None:
        async for frame in client.events_stream("s", follow=False):
            frames.append(frame)

    task = asyncio.create_task(collect())
    for _ in range(200):
        if client._streams:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("stream never subscribed")
    sink = next(iter(client._streams.values()))
    sink.put(_event())
    sink.put(_END)
    await asyncio.wait_for(task, 5)
    return frames


def test_ack_connection_close_is_ignored() -> None:
    socket = _FakeSocket(ack_error=ConnectionClosed(None, None))
    client = _client(socket)
    frames = asyncio.run(_consume(client))
    assert [frame.event.sequence for frame in frames] == [4]
    assert socket.methods == []


def test_ack_non_close_failure_still_propagates() -> None:
    socket = _FakeSocket(ack_error=RuntimeError("not a close"))
    client = _client(socket)
    with pytest.raises(RuntimeError, match="not a close"):
        asyncio.run(_consume(client))
