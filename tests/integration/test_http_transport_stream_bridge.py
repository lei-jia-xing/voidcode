"""Integration tests for the run-stream worker bridge.

``RuntimeTransportApp._stream_runtime_chunks`` hands the runtime's synchronous
stream generator to a worker thread and posts chunks back to the event loop. The
rest of the HTTP transport suite replaces that bridge with a direct async
generator, so these tests are the only ones that exercise the real hand-off: the
ordering guarantee, the disconnect teardown, and the reason the bridge does not
borrow the loop's shared default executor.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

from voidcode.runtime.contracts import RuntimeRequest, RuntimeStreamChunk
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.transport.http import RuntimeTransport, RuntimeTransportApp

_WORKER_THREAD_NAME = "runtime-stream-worker"


class _StubRuntime:
    """Runtime double whose stream is slow enough to observe the bridge."""

    def __init__(self, *, total: int = 500, delay: float = 0.0, gate: threading.Event | None = None) -> None:
        self.total = total
        self.delay = delay
        self.gate = gate
        self.produced = 0
        self.cancelled: list[tuple[str, str | None]] = []

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        session = SessionState(
            session=SessionRef(id=cast(str, request.session_id)),
            status="running",
            turn=1,
            metadata={},
        )
        for sequence in range(1, self.total + 1):
            if sequence > 1:
                if self.gate is not None:
                    _ = self.gate.wait(timeout=2.0)
                elif self.delay:
                    time.sleep(self.delay)
            self.produced = sequence
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=cast(str, request.session_id),
                    sequence=sequence,
                    event_type="graph.provider_stream",
                    source="graph",
                    payload={"sequence": sequence},
                ),
            )

    def cancel_session(self, session_id: str, *, run_id: str | None = None, reason: str | None = None) -> object:
        self.cancelled.append((session_id, reason))
        return SimpleNamespace(interrupted=False, as_payload=lambda: {})

    def __exit__(self, *_: object) -> None:
        return None


def _documented_request() -> RuntimeRequest:
    return RuntimeRequest(prompt="bridge me", session_id="bridge-session")


def _run_stream_request_body() -> bytes:
    return json.dumps({"prompt": "bridge me", "session_id": "bridge-session"}).encode("utf-8")


def _drive(app: RuntimeTransportApp, *, receive: Any, send: Any) -> None:
    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/api/runtime/run/stream",
        "query_string": b"",
        "headers": [],
    }
    asyncio.run(app(scope, receive, send))


def _data_frames(sent: list[dict[str, object]]) -> list[dict[str, object]]:
    parts = [cast(bytes, message.get("body", b"")) for message in sent if message["type"] == "http.response.body"]
    return [cast(dict[str, object], json.loads(part.removeprefix(b"data: "))) for part in parts if part.startswith(b"data: ")]


def _live_workers() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == _WORKER_THREAD_NAME]


def _wait_for_workers_to_exit(timeout: float = 2.0) -> list[threading.Thread]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        workers = _live_workers()
        if not workers:
            return []
        time.sleep(0.01)
    return _live_workers()


def test_stream_bridge_forwards_every_chunk_in_order() -> None:
    runtime = _StubRuntime(total=25)
    app = RuntimeTransportApp(runtime_factory=lambda: cast(RuntimeTransport, runtime))
    messages: list[dict[str, object]] = [{"type": "http.request", "body": _run_stream_request_body(), "more_body": False}]
    sent: list[dict[str, object]] = []
    hang = asyncio.Event()

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        await hang.wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    _drive(app, receive=_receive, send=_send)

    payloads = _data_frames(sent)
    assert [payload["kind"] for payload in payloads] == ["event"] * 25
    assert [cast(dict[str, object], payload["event"])["sequence"] for payload in payloads] == list(range(1, 26))
    # Every chunk and nothing else: the bridge does not duplicate or drop frames.
    assert runtime.produced == 25
    assert _live_workers() == []


def test_stream_bridge_stops_the_worker_when_the_client_disconnects() -> None:
    # A long run with a gap between chunks, dropped by the client mid-stream.
    runtime = _StubRuntime(total=10_000, delay=0.002)
    app = RuntimeTransportApp(runtime_factory=lambda: cast(RuntimeTransport, runtime))
    messages: list[dict[str, object]] = [{"type": "http.request", "body": _run_stream_request_body(), "more_body": False}]
    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)
        # A real ASGI send suspends on the socket write.
        await asyncio.sleep(0)

    _drive(app, receive=_receive, send=_send)

    assert len(_data_frames(sent)) < runtime.total
    assert runtime.cancelled == [("bridge-session", "client_disconnected")]
    # The producer thread must not keep draining a run nobody is reading.
    assert _wait_for_workers_to_exit() == []
    assert runtime.produced < runtime.total


def test_stream_bridge_keeps_the_default_executor_free() -> None:
    """Live streams must not occupy the event loop's shared default executor.

    Each stream parks one worker thread in the runtime, never one of the
    ``asyncio.to_thread`` slots the resume/approval/question handlers share, so
    more concurrent streams than the executor has threads must leave it usable.
    """
    concurrency = min(32, (os.cpu_count() or 1) + 4) + 4

    async def scenario() -> None:
        app = RuntimeTransportApp(runtime_factory=lambda: None)
        gate = threading.Event()
        streams = []
        for _ in range(concurrency):
            runtime = _StubRuntime(total=100, gate=gate)
            stream = app._stream_runtime_chunks(cast(RuntimeTransport, runtime), _documented_request())
            assert await anext(stream) is not None
            streams.append(stream)
        # Every stream now waits for its next chunk at once.
        waiting = [asyncio.ensure_future(anext(stream)) for stream in streams]
        await asyncio.sleep(0.1)
        try:
            assert len(_live_workers()) == concurrency
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "executor is free"), timeout=1.0) == "executor is free"
        finally:
            for task in waiting:
                _ = task.cancel()
            _ = await asyncio.gather(*waiting, return_exceptions=True)
            gate.set()
            for stream in streams:
                await stream.aclose()

    asyncio.run(scenario())
    assert _wait_for_workers_to_exit() == []


class _BoomExitRuntime(_StubRuntime):
    """Runtime whose teardown always fails, like a broken embedding."""

    def __exit__(self, *_: object) -> None:
        raise RuntimeError("runtime teardown failed")


def test_stream_bridge_cancels_the_run_even_when_runtime_teardown_fails() -> None:
    """A dropped stream must cancel the run, not lose the cancel to teardown.

    The runtime is coordinator-less here, so the transport owns closing it; a
    raising ``__exit__`` must not be able to run before (or instead of) the
    ``client_disconnected`` cancel.
    """
    runtime = _BoomExitRuntime(total=10_000, delay=0.002)
    app = RuntimeTransportApp(runtime_factory=lambda: cast(RuntimeTransport, runtime))
    messages: list[dict[str, object]] = [{"type": "http.request", "body": _run_stream_request_body(), "more_body": False}]
    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)
        # A real ASGI send suspends on the socket write.
        await asyncio.sleep(0)

    _drive(app, receive=_receive, send=_send)

    assert runtime.cancelled == [("bridge-session", "client_disconnected")]
    assert len(_data_frames(sent)) < runtime.total
    assert _wait_for_workers_to_exit() == []


def test_stream_bridge_does_not_cancel_a_completed_run_with_failing_teardown() -> None:
    """A stream that ran to its last frame cancels nothing, teardown or not."""
    runtime = _BoomExitRuntime(total=5)
    app = RuntimeTransportApp(runtime_factory=lambda: cast(RuntimeTransport, runtime))
    messages: list[dict[str, object]] = [{"type": "http.request", "body": _run_stream_request_body(), "more_body": False}]
    sent: list[dict[str, object]] = []
    hang = asyncio.Event()

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        await hang.wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    _drive(app, receive=_receive, send=_send)

    assert runtime.cancelled == []
    assert runtime.produced == 5
    assert len(_data_frames(sent)) == 5
    assert _wait_for_workers_to_exit() == []


def test_stream_bridge_reports_runtime_failures_to_the_caller() -> None:
    """A runtime that raises before yielding must surface as a failed request."""

    class _BoomRuntime:
        def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover - marks this as a generator function

        def cancel_session(self, session_id: str, *, run_id: str | None = None, reason: str | None = None) -> object:
            raise AssertionError("a run that never started must not be cancelled")

        def __exit__(self, *_: object) -> None:
            return None

    runtime = _BoomRuntime()
    app = RuntimeTransportApp(runtime_factory=lambda: cast(RuntimeTransport, runtime))
    messages: list[dict[str, object]] = [{"type": "http.request", "body": _run_stream_request_body(), "more_body": False}]
    sent: list[dict[str, object]] = []
    hang = asyncio.Event()

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        await hang.wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    _drive(app, receive=_receive, send=_send)

    start_message = next(message for message in sent if message["type"] == "http.response.start")
    assert start_message["status"] == 500
    body = b"".join(cast(bytes, message.get("body", b"")) for message in sent if message["type"] == "http.response.body")
    assert json.loads(body) == {"error": "internal server error", "code": None}
    assert _live_workers() == []


def test_run_stream_request_reaches_the_runtime_unchanged() -> None:
    """The bridge must hand the runtime the exact request the transport built."""
    seen: list[RuntimeRequest] = []

    class _CapturingRuntime:
        def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
            seen.append(request)
            yield RuntimeStreamChunk(
                kind="output",
                session=SessionState(session=SessionRef(id="bridge-session"), status="completed", turn=1, metadata={}),
                output="done",
            )

        def __exit__(self, *_: object) -> None:
            return None

    runtime = _CapturingRuntime()
    app = RuntimeTransportApp(runtime_factory=lambda: cast(RuntimeTransport, runtime))
    messages: list[dict[str, object]] = [
        {
            "type": "http.request",
            "body": json.dumps({"prompt": "bridge me", "session_id": "bridge-session", "metadata": {"provider_stream": True}}).encode("utf-8"),
            "more_body": False,
        }
    ]
    sent: list[dict[str, object]] = []
    hang = asyncio.Event()

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        await hang.wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    _drive(app, receive=_receive, send=_send)

    assert [(request.prompt, request.session_id, request.metadata, request.allocate_session_id) for request in seen] == [
        ("bridge me", "bridge-session", {"provider_stream": True}, False)
    ]
    assert _data_frames(sent)[0]["output"] == "done"
