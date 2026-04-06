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
from unittest.mock import patch

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

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: str | None = None,
    ) -> RuntimeResponseLike: ...


class RuntimeFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        tool_registry: object | None = None,
        graph: object | None = None,
        permission_policy: object | None = None,
        session_store: object | None = None,
    ) -> RuntimeRunner: ...


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


def _run_lifespan(app: TransportAppLike) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "lifespan.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    asyncio.run(app({"type": "lifespan"}, _receive, _send))
    return sent


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


def test_create_runtime_app_forwards_config_to_default_runtime_factory(tmp_path: Path) -> None:
    runtime_module = importlib.import_module("voidcode.runtime.http")
    config = object()
    captured: list[tuple[Path, object | None]] = []

    class StubRuntime:
        def __init__(self, *, workspace: Path, config: object | None = None) -> None:
            captured.append((workspace, config))

        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def resume(self, session_id: str, **_: object) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    with patch.object(runtime_module, "VoidCodeRuntime", StubRuntime):
        app = runtime_module.create_runtime_app(workspace=tmp_path, config=config)
        _ = app._runtime_factory()

    assert captured == [(tmp_path, config)]


def test_transport_handles_lifespan_startup_and_shutdown(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    sent = _run_lifespan(app)

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


def test_transport_rejects_other_unsupported_scope_types(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    error = _run_non_http_scope(app, "websocket")

    assert str(error) == "unsupported scope type: 'websocket'"


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
        "runtime.skills_loaded",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]


def test_transport_resolves_pending_approval_allow_over_http(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    waiting = runtime.run(
        runtime_request(prompt="write danger.txt approved later", session_id="approval-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/approval-session/approval",
        body=json.dumps(
            {
                "request_id": approval_request_id,
                "decision": "allow",
            }
        ).encode("utf-8"),
    )
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert payload["session"] == {
        "session": {"id": "approval-session"},
        "status": "completed",
        "turn": 1,
        "metadata": {"workspace": str(tmp_path)},
    }
    assert payload["output"] == "approved later"
    assert [event["event_type"] for event in cast(list[dict[str, object]], payload["events"])] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved later"


def test_transport_resolves_pending_approval_deny_over_http(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    waiting = runtime.run(
        runtime_request(prompt="write danger.txt denied later", session_id="deny-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/deny-session/approval",
        body=json.dumps(
            {
                "request_id": approval_request_id,
                "decision": "deny",
            }
        ).encode("utf-8"),
    )
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert payload["session"] == {
        "session": {"id": "deny-session"},
        "status": "failed",
        "turn": 1,
        "metadata": {"workspace": str(tmp_path)},
    }
    assert payload["output"] is None
    assert [event["event_type"] for event in cast(list[dict[str, object]], payload["events"])] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert (tmp_path / "danger.txt").exists() is False


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (b"not json", "request body must be valid JSON"),
        (json.dumps(["allow"]).encode("utf-8"), "request body must be a JSON object"),
        (
            json.dumps({"request_id": "req-1", "decision": "maybe"}).encode("utf-8"),
            "decision must be 'allow' or 'deny'",
        ),
        (
            json.dumps({"decision": "allow"}).encode("utf-8"),
            "request_id must be a non-empty string",
        ),
    ],
)
def test_transport_rejects_invalid_approval_resolution_payload(
    tmp_path: Path,
    body: bytes,
    expected_error: str,
) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/approval-session/approval",
        body=body,
    )

    assert response.status == 400
    assert response.json() == {"error": expected_error}


def test_transport_returns_not_found_when_approval_resolution_has_no_pending_request(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("http replay\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="completed-session"))

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/completed-session/approval",
        body=json.dumps(
            {
                "request_id": "missing-request",
                "decision": "allow",
            }
        ).encode("utf-8"),
    )

    assert response.status == 404
    assert response.json() == {"error": "no pending approval for session: completed-session"}


def test_transport_rejects_non_post_method_for_approval_resolution_route(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(app, method="GET", path="/api/sessions/approval-session/approval")

    assert response.status == 405
    assert response.json() == {"error": "method not allowed"}


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


def test_transport_serializes_additive_future_event_type_unchanged() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    events_module = importlib.import_module("voidcode.runtime.events")
    future_event_type = cast(str, events_module.RUNTIME_MEMORY_REFRESHED)
    session = session_state(
        session=session_ref(id="future-event-session"),
        status="running",
        turn=1,
        metadata={"workspace": "/tmp/workspace"},
    )

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "future event please"
            yield runtime_stream_chunk(
                kind="event",
                session=session,
                event=event_envelope(
                    session_id="future-event-session",
                    sequence=1,
                    event_type=future_event_type,
                    source="runtime",
                    payload={"count": 1},
                ),
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(
        workspace=Path("/tmp/workspace"),
        runtime_factory=lambda: StubRuntime(),
    )

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "future event please"}).encode("utf-8"),
    )
    payloads = _parse_sse_payloads(response)

    assert response.status == 200
    assert cast(dict[str, object], payloads[0]["event"])["event_type"] == future_event_type


def test_transport_persists_streamed_run_for_session_listing_and_replay(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("stream replay\n", encoding="utf-8")
    create_runtime_app = _load_transport_app_factory()

    app = create_runtime_app(workspace=tmp_path)

    stream_response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": "read sample.txt",
                "session_id": "streamed-session",
            }
        ).encode("utf-8"),
    )
    stream_payloads = _parse_sse_payloads(stream_response)

    list_response = _run_app(app, method="GET", path="/api/sessions")
    replay_response = _run_app(app, method="GET", path="/api/sessions/streamed-session")
    replay_payload = cast(dict[str, object], replay_response.json())

    assert stream_response.status == 200
    assert [payload["kind"] for payload in stream_payloads] == [
        "event",
        "event",
        "event",
        "event",
        "event",
        "event",
        "event",
        "output",
    ]
    assert list_response.status == 200
    assert list_response.json() == [
        {
            "session": {"id": "streamed-session"},
            "status": "completed",
            "turn": 1,
            "prompt": "read sample.txt",
            "updated_at": 1,
        }
    ]
    assert replay_response.status == 200
    assert replay_payload["session"] == {
        "session": {"id": "streamed-session"},
        "status": "completed",
        "turn": 1,
        "metadata": {"workspace": str(tmp_path)},
    }
    assert replay_payload["output"] == "stream replay\n"
    assert [
        event["event_type"] for event in cast(list[dict[str, object]], replay_payload["events"])
    ] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]


def test_transport_allocates_distinct_anonymous_stream_sessions(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("anonymous stream\n", encoding="utf-8")
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    first_stream_response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "read sample.txt"}).encode("utf-8"),
    )
    second_stream_response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "read sample.txt"}).encode("utf-8"),
    )
    first_payloads = _parse_sse_payloads(first_stream_response)
    second_payloads = _parse_sse_payloads(second_stream_response)

    first_session_id = cast(
        str,
        cast(dict[str, object], cast(dict[str, object], first_payloads[0]["session"])["session"])[
            "id"
        ],
    )
    second_session_id = cast(
        str,
        cast(dict[str, object], cast(dict[str, object], second_payloads[0]["session"])["session"])[
            "id"
        ],
    )

    list_response = _run_app(app, method="GET", path="/api/sessions")
    listed_sessions = cast(list[dict[str, object]], list_response.json())
    listed_session_ids = [
        cast(str, cast(dict[str, object], item["session"])["id"]) for item in listed_sessions
    ]

    first_replay_response = _run_app(app, method="GET", path=f"/api/sessions/{first_session_id}")
    second_replay_response = _run_app(app, method="GET", path=f"/api/sessions/{second_session_id}")
    first_replay_payload = cast(dict[str, object], first_replay_response.json())
    second_replay_payload = cast(dict[str, object], second_replay_response.json())

    assert first_stream_response.status == 200
    assert second_stream_response.status == 200
    assert first_session_id.startswith("session-")
    assert second_session_id.startswith("session-")
    assert first_session_id != second_session_id
    assert list_response.status == 200
    assert listed_session_ids == [second_session_id, first_session_id]
    assert [item["prompt"] for item in listed_sessions] == ["read sample.txt", "read sample.txt"]
    assert first_replay_response.status == 200
    assert second_replay_response.status == 200
    assert first_replay_payload["session"] == {
        "session": {"id": first_session_id},
        "status": "completed",
        "turn": 1,
        "metadata": {"workspace": str(tmp_path)},
    }
    assert second_replay_payload["session"] == {
        "session": {"id": second_session_id},
        "status": "completed",
        "turn": 1,
        "metadata": {"workspace": str(tmp_path)},
    }
    assert first_replay_payload["output"] == "anonymous stream\n"
    assert second_replay_payload["output"] == "anonymous stream\n"


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


def test_transport_persists_failed_stream_for_replay(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("broken\n", encoding="utf-8")
    create_runtime_app = _load_transport_app_factory()
    read_file_module = importlib.import_module("voidcode.tools.read_file")
    read_file_tool = cast(type[object], read_file_module.ReadFileTool)

    def _failing_invoke(_self: object, _call: object, *, workspace: Path) -> object:
        _ = workspace
        raise ValueError("boom from transport stream")

    app = create_runtime_app(workspace=tmp_path)

    with patch.object(read_file_tool, "invoke", autospec=True, side_effect=_failing_invoke):
        stream_response = _run_app(
            app,
            method="POST",
            path="/api/runtime/run/stream",
            body=json.dumps(
                {
                    "prompt": "read sample.txt",
                    "session_id": "failed-stream-session",
                }
            ).encode("utf-8"),
        )

    payloads = _parse_sse_payloads(stream_response)
    list_response = _run_app(app, method="GET", path="/api/sessions")
    replay_response = _run_app(app, method="GET", path="/api/sessions/failed-stream-session")
    replay_payload = cast(dict[str, object], replay_response.json())

    assert stream_response.status == 200
    assert payloads[-1]["event"] == {
        "session_id": "failed-stream-session",
        "sequence": 6,
        "event_type": "runtime.failed",
        "source": "runtime",
        "payload": {"error": "boom from transport stream"},
    }
    assert list_response.json() == [
        {
            "session": {"id": "failed-stream-session"},
            "status": "failed",
            "turn": 1,
            "prompt": "read sample.txt",
            "updated_at": 1,
        }
    ]
    assert replay_response.status == 200
    assert replay_payload["session"] == {
        "session": {"id": "failed-stream-session"},
        "status": "failed",
        "turn": 1,
        "metadata": {"workspace": str(tmp_path)},
    }
    assert replay_payload["output"] is None
    assert [
        event["event_type"] for event in cast(list[dict[str, object]], replay_payload["events"])
    ] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.failed",
    ]


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


def test_transport_rejects_unreplayable_session_id_in_run_stream_payload() -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=Path("/tmp/workspace"))

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "read sample.txt", "session_id": "bad/session"}).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json() == {"error": "session_id must not contain '/'"}


def test_transport_returns_not_found_for_unknown_session(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(app, method="GET", path="/api/sessions/missing-session")

    assert response.status == 404
    assert response.json() == {"error": "unknown session: missing-session"}


def test_transport_returns_not_found_for_unaddressable_session_id(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(app, method="GET", path="/api/sessions/bad/session")

    assert response.status == 404
    assert response.json() == {"error": "not found"}
