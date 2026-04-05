from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pytest


class SessionRefLike(Protocol):
    id: str


class SessionLike(Protocol):
    session: SessionRefLike
    status: str
    turn: int
    metadata: dict[str, object]


class EventLike(Protocol):
    session_id: str
    sequence: int
    event_type: str
    source: str
    payload: dict[str, object]


class StreamChunkLike(Protocol):
    kind: str
    session: SessionLike
    event: EventLike | None
    output: str | None


class RuntimeResponseLike(Protocol):
    session: SessionLike
    events: tuple[EventLike, ...]
    output: str | None


class RuntimeRequestLike(Protocol):
    prompt: str
    session_id: str | None
    metadata: dict[str, object]


class StoredSessionSummaryLike(Protocol):
    session: SessionRefLike
    status: str
    turn: int
    prompt: str
    updated_at: int


class RuntimeRunner(Protocol):
    def run(self, request: RuntimeRequestLike) -> RuntimeResponseLike: ...

    def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]: ...

    def resume(self, session_id: str) -> RuntimeResponseLike: ...


class RuntimeFactory(Protocol):
    def __call__(self, *, workspace: Path) -> RuntimeRunner: ...


class RuntimeRequestFactory(Protocol):
    def __call__(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RuntimeRequestLike: ...


class RuntimeStreamChunkFactory(Protocol):
    def __call__(
        self,
        *,
        kind: str,
        session: object,
        event: object | None = None,
        output: str | None = None,
    ) -> StreamChunkLike: ...


class SessionRefFactory(Protocol):
    def __call__(self, *, id: str) -> SessionRefLike: ...


class SessionStateFactory(Protocol):
    def __call__(
        self,
        *,
        session: object,
        status: str,
        turn: int,
        metadata: dict[str, object] | None = None,
    ) -> SessionLike: ...


class EventEnvelopeFactory(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        sequence: int,
        event_type: str,
        source: str,
        payload: dict[str, object] | None = None,
    ) -> EventLike: ...


class Receive(Protocol):
    async def __call__(self) -> dict[str, object]: ...


class Send(Protocol):
    async def __call__(self, message: dict[str, object]) -> None: ...


class TransportAppLike(Protocol):
    async def __call__(
        self,
        scope: dict[str, object],
        receive: Receive,
        send: Send,
    ) -> None: ...


class TransportAppFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        runtime_factory: object | None = None,
    ) -> TransportAppLike: ...


sys_path = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(sys_path))


def _load_transport_app_factory() -> TransportAppFactory:
    runtime_module = importlib.import_module("voidcode.runtime")
    return cast(TransportAppFactory, runtime_module.create_runtime_app)


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)
    runtime_class = cast(RuntimeFactory, service_module.VoidCodeRuntime)
    return runtime_request, runtime_class


def _load_stream_types() -> tuple[
    RuntimeStreamChunkFactory,
    SessionRefFactory,
    SessionStateFactory,
    EventEnvelopeFactory,
]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    session_module = importlib.import_module("voidcode.runtime.session")
    events_module = importlib.import_module("voidcode.runtime.events")
    return (
        cast(RuntimeStreamChunkFactory, contracts_module.RuntimeStreamChunk),
        cast(SessionRefFactory, session_module.SessionRef),
        cast(SessionStateFactory, session_module.SessionState),
        cast(EventEnvelopeFactory, events_module.EventEnvelope),
    )


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    status: int
    headers: dict[str, str]
    body_parts: list[bytes]

    @property
    def body(self) -> bytes:
        return b"".join(self.body_parts)

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


def _run_app(
    app: TransportAppLike,
    *,
    method: str,
    path: str,
    body: bytes = b"",
) -> _TransportResponse:
    messages: list[dict[str, object]] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": method,
        "path": path,
    }
    asyncio.run(app(scope, _receive, _send))

    start_message = next(
        message for message in sent if cast(str, message["type"]) == "http.response.start"
    )
    headers = {
        key.decode("utf-8").lower(): value.decode("utf-8")
        for key, value in cast(list[tuple[bytes, bytes]], start_message["headers"])
    }
    body_parts = [
        cast(bytes, message.get("body", b""))
        for message in sent
        if cast(str, message["type"]) == "http.response.body"
    ]
    return _TransportResponse(
        status=cast(int, start_message["status"]),
        headers=headers,
        body_parts=body_parts,
    )


def _parse_sse_payloads(response: _TransportResponse) -> list[dict[str, object]]:
    frames = [frame for frame in response.body.decode("utf-8").split("\n\n") if frame]
    payloads: list[dict[str, object]] = []
    for frame in frames:
        prefix = "data: "
        assert frame.startswith(prefix)
        payloads.append(cast(dict[str, object], json.loads(frame[len(prefix) :])))
    return payloads


def _run_non_http_scope(app: TransportAppLike, scope_type: str) -> RuntimeError:
    async def _receive() -> dict[str, object]:
        return {"type": f"{scope_type}.startup"}

    async def _send(message: dict[str, object]) -> None:
        raise AssertionError(f"send should not be called for {scope_type!r}: {message}")

    try:
        asyncio.run(app({"type": scope_type}, _receive, _send))
    except RuntimeError as exc:
        return exc

    raise AssertionError(f"expected RuntimeError for unsupported scope {scope_type!r}")


def test_transport_lists_sessions_as_json(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("http list\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="transport-session"))

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(app, method="GET", path="/api/sessions")

    assert response.status == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == [
        {
            "session": {"id": "transport-session"},
            "status": "completed",
            "turn": 1,
            "prompt": "read sample.txt",
            "updated_at": 1,
        }
    ]


def test_transport_rejects_unsupported_lifespan_scope(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    error = _run_non_http_scope(app, "lifespan")

    assert str(error) == "unsupported scope type: 'lifespan'"


def test_transport_replays_session_as_json_runtime_response(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("http replay\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()

    runtime = runtime_class(workspace=tmp_path)
    stored = runtime.run(runtime_request(prompt="read sample.txt", session_id="transport-session"))

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(app, method="GET", path="/api/sessions/transport-session")
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert payload["output"] == "http replay\n"
    assert payload["session"] == {
        "session": {"id": "transport-session"},
        "status": stored.session.status,
        "turn": stored.session.turn,
        "metadata": stored.session.metadata,
    }
    assert [event["event_type"] for event in cast(list[dict[str, object]], payload["events"])] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]


def test_transport_streams_runtime_chunks_in_sse_order() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    session = session_state(
        session=session_ref(id="stream-session"),
        status="running",
        turn=1,
        metadata={"workspace": "/tmp/workspace"},
    )
    completed_session = session_state(
        session=session_ref(id="stream-session"),
        status="completed",
        turn=1,
        metadata={"workspace": "/tmp/workspace"},
    )

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "transport me"
            assert request.session_id == "stream-session"
            assert request.metadata == {"client": "transport-test"}
            yield runtime_stream_chunk(
                kind="event",
                session=session,
                event=event_envelope(
                    session_id="stream-session",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": request.prompt},
                ),
            )
            yield runtime_stream_chunk(
                kind="event",
                session=completed_session,
                event=event_envelope(
                    session_id="stream-session",
                    sequence=2,
                    event_type="graph.response_ready",
                    source="graph",
                    payload={"output_preview": "transported"},
                ),
            )
            yield runtime_stream_chunk(
                kind="output",
                session=completed_session,
                output="transported",
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(
        workspace=Path("/tmp/workspace"), runtime_factory=lambda: StubRuntime()
    )

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": "transport me",
                "session_id": "stream-session",
                "metadata": {"client": "transport-test"},
            }
        ).encode("utf-8"),
    )
    payloads = _parse_sse_payloads(response)

    assert response.status == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert len(payloads) == 3
    assert [payload["kind"] for payload in payloads] == ["event", "event", "output"]
    assert [
        cast(dict[str, object], payload["event"])["event_type"]
        for payload in payloads
        if payload["event"] is not None
    ] == ["runtime.request_received", "graph.response_ready"]
    assert payloads[-1]["output"] == "transported"


def test_transport_stream_preserves_failed_chunk_before_termination() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    failed_session = session_state(
        session=session_ref(id="failed-session"),
        status="failed",
        turn=1,
        metadata={"workspace": "/tmp/workspace"},
    )

    class FailingStubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "fail me"
            yield runtime_stream_chunk(
                kind="event",
                session=failed_session,
                event=event_envelope(
                    session_id="failed-session",
                    sequence=1,
                    event_type="runtime.failed",
                    source="runtime",
                    payload={"error": "boom from stream"},
                ),
            )
            raise RuntimeError("boom from stream")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(
        workspace=Path("/tmp/workspace"),
        runtime_factory=lambda: FailingStubRuntime(),
    )

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "fail me"}).encode("utf-8"),
    )
    payloads = _parse_sse_payloads(response)

    assert response.status == 200
    assert payloads == [
        {
            "kind": "event",
            "session": {
                "session": {"id": "failed-session"},
                "status": "failed",
                "turn": 1,
                "metadata": {"workspace": "/tmp/workspace"},
            },
            "event": {
                "session_id": "failed-session",
                "sequence": 1,
                "event_type": "runtime.failed",
                "source": "runtime",
                "payload": {"error": "boom from stream"},
            },
            "output": None,
        }
    ]
    assert response.body.endswith(b"\n\n")


def test_transport_logs_unexpected_streaming_errors(caplog: pytest.LogCaptureFixture) -> None:
    create_runtime_app = _load_transport_app_factory()

    class ExplodingRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "explode"
            raise RuntimeError("serialization exploded")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(
        workspace=Path("/tmp/workspace"),
        runtime_factory=lambda: ExplodingRuntime(),
    )

    with caplog.at_level(logging.ERROR):
        response = _run_app(
            app,
            method="POST",
            path="/api/runtime/run/stream",
            body=json.dumps({"prompt": "explode"}).encode("utf-8"),
        )

    assert response.status == 200
    assert response.body == b""
    assert "unexpected transport streaming failure" in caplog.text


def test_transport_rejects_invalid_run_stream_payload() -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=Path("/tmp/workspace"))

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": 123}).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json() == {"error": "prompt must be a non-empty string"}


def test_transport_rejects_empty_session_id_in_run_stream_payload() -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=Path("/tmp/workspace"))

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "read sample.txt", "session_id": ""}).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json() == {"error": "session_id must be a non-empty string when provided"}


def test_transport_returns_not_found_for_unknown_session(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(app, method="GET", path="/api/sessions/missing-session")

    assert response.status == 404
    assert response.json() == {"error": "unknown session: missing-session"}
