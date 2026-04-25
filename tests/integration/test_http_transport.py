from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import patch

import pytest


def _cwd_command() -> str:
    return f'"{sys.executable}" -c "import os; print(os.getcwd())"'


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


class QuestionResponseLike(Protocol):
    header: str
    answers: tuple[object, ...]


class StoredSessionSummaryLike(Protocol):
    session: SessionRefLike
    status: str
    turn: int
    prompt: str
    updated_at: int


class RuntimeRunner(Protocol):
    def run(self, request: RuntimeRequestLike) -> RuntimeResponseLike: ...

    def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]: ...

    def web_settings(self) -> dict[str, object]: ...

    def update_web_settings(
        self,
        *,
        provider: str | None = None,
        provider_api_key: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]: ...

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: str | None = None,
    ) -> RuntimeResponseLike: ...

    def answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[object, ...],
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


def _assert_runtime_session_metadata(
    metadata: object,
    *,
    workspace: Path | str,
    approval_mode: str = "ask",
    model: str | None = None,
    execution_engine: str = "deterministic",
) -> None:
    assert isinstance(metadata, dict)
    typed_metadata = cast(dict[str, object], metadata)
    assert typed_metadata["workspace"] == str(workspace)

    raw_runtime_config = typed_metadata.get("runtime_config")
    assert isinstance(raw_runtime_config, dict)
    runtime_config = cast(dict[str, object], raw_runtime_config)
    assert runtime_config["approval_mode"] == approval_mode
    assert runtime_config["execution_engine"] == execution_engine
    if model is None:
        assert "model" not in runtime_config
    else:
        assert runtime_config["model"] == model


def _multi_step_prompt() -> str:
    return "read source.txt\nwrite copied.txt copied marker\ngrep copied copied.txt"


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


def test_transport_reads_runtime_web_settings_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(app, method="GET", path="/api/settings")
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert payload == {
        "provider": None,
        "provider_api_key_present": False,
        "model": None,
    }


def test_transport_updates_runtime_web_settings_and_hides_api_key_on_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    update_response = _run_app(
        app,
        method="POST",
        path="/api/settings",
        body=json.dumps(
            {
                "provider": "opencode-go",
                "provider_api_key": "secret-key",
                "model": "opencode-go/glm-5.1",
            }
        ).encode("utf-8"),
    )
    update_payload = cast(dict[str, object], update_response.json())
    read_response = _run_app(app, method="GET", path="/api/settings")
    read_payload = cast(dict[str, object], read_response.json())

    assert update_response.status == 200
    assert update_payload == {
        "provider": "opencode-go",
        "provider_api_key_present": True,
        "model": "opencode-go/glm-5.1",
    }
    assert read_response.status == 200
    assert read_payload == update_payload


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        (b"not json", "request body must be valid JSON"),
        (json.dumps(["glm"]).encode("utf-8"), "request body must be a JSON object"),
        (
            json.dumps({"provider": 1}).encode("utf-8"),
            "provider must be a string when provided",
        ),
        (
            json.dumps({"extra": True}).encode("utf-8"),
            "unsupported settings field(s): extra",
        ),
    ],
)
def test_transport_rejects_invalid_settings_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    expected_error: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(app, method="POST", path="/api/settings", body=body)

    assert response.status == 400
    assert response.json() == {"error": expected_error}


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

        def web_settings(self) -> dict[str, object]:
            return {"provider": None, "provider_api_key_present": False, "model": None}

        def update_web_settings(self, **_: object) -> dict[str, object]:
            return {"provider": None, "provider_api_key_present": False, "model": None}

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


def test_transport_closes_request_scoped_runtime_after_list_sessions(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    closed: list[str] = []

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

        def session_result(self, *, session_id: str) -> object:
            raise AssertionError(f"session_result should not be called: {session_id}")

        def list_notifications(self) -> tuple[object, ...]:
            raise AssertionError("list_notifications should not be called")

        def acknowledge_notification(self, *, notification_id: str) -> object:
            raise AssertionError(
                f"acknowledge_notification should not be called: {notification_id}"
            )

        def resume(self, session_id: str, **_: object) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = exc_type, exc, tb
            closed.append("closed")

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: StubRuntime())

    response = _run_app(app, method="GET", path="/api/sessions")

    assert response.status == 200
    assert response.json() == []
    assert closed == ["closed"]


def test_transport_closes_request_scoped_runtime_after_stream_run(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    closed: list[str] = []
    running_session = session_state(
        session=session_ref(id="stream-close-session"),
        status="running",
        turn=1,
        metadata={"workspace": str(tmp_path)},
    )
    completed_session = session_state(
        session=session_ref(id="stream-close-session"),
        status="completed",
        turn=1,
        metadata={"workspace": str(tmp_path)},
    )

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "close after stream"
            yield runtime_stream_chunk(
                kind="event",
                session=running_session,
                event=event_envelope(
                    session_id="stream-close-session",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": "close after stream"},
                ),
            )
            yield runtime_stream_chunk(
                kind="output",
                session=completed_session,
                output="done",
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = exc_type, exc, tb
            closed.append("closed")

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: StubRuntime())

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "close after stream"}).encode("utf-8"),
    )

    assert response.status == 200
    assert closed == ["closed"]


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
    assert payload["output"] == "http replay"
    assert payload["session"] == {
        "session": {"id": "transport-session"},
        "status": stored.session.status,
        "turn": stored.session.turn,
        "metadata": stored.session.metadata,
    }
    assert [event["event_type"] for event in cast(list[dict[str, object]], payload["events"])] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]


def test_transport_reads_session_result_with_transcript(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("result payload\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()

    runtime = runtime_class(workspace=tmp_path)
    stored = runtime.run(runtime_request(prompt="read sample.txt", session_id="result-session"))

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(app, method="GET", path="/api/sessions/result-session/result")
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert payload["prompt"] == "read sample.txt"
    assert payload["status"] == "completed"
    assert payload["summary"] == "Completed: result payload"
    assert payload["output"] == "result payload"
    assert payload["error"] is None
    assert payload["last_event_sequence"] == stored.events[-1].sequence
    assert [
        event["event_type"] for event in cast(list[dict[str, object]], payload["transcript"])
    ] == [event.event_type for event in stored.events]


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
    assert cast(dict[str, object], payload["session"])["session"] == {"id": "approval-session"}
    assert cast(dict[str, object], payload["session"])["status"] == "completed"
    assert cast(dict[str, object], payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert payload["output"] == "approved later"
    assert [event["event_type"] for event in cast(list[dict[str, object]], payload["events"])] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved later"


def test_transport_lists_and_acknowledges_notifications(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    _ = runtime.run(
        runtime_request(prompt="write danger.txt approved later", session_id="notification-session")
    )

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    list_response = _run_app(app, method="GET", path="/api/notifications")
    notifications = cast(list[dict[str, object]], list_response.json())

    assert list_response.status == 200
    assert len(notifications) == 1
    assert notifications[0]["kind"] == "approval_blocked"
    assert notifications[0]["status"] == "unread"

    notification_id = cast(str, notifications[0]["id"])
    ack_response = _run_app(app, method="POST", path=f"/api/notifications/{notification_id}/ack")
    ack_payload = cast(dict[str, object], ack_response.json())

    assert ack_response.status == 200
    assert ack_payload["id"] == notification_id
    assert ack_payload["status"] == "acknowledged"
    assert ack_payload["acknowledged_at"] is not None


def test_transport_round_trips_parent_session_lineage(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_module = importlib.import_module("voidcode.runtime")

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "child task"
            assert getattr(request, "parent_session_id", None) == "leader-session"
            yield runtime_module.RuntimeStreamChunk(
                kind="output",
                session=runtime_module.SessionState(
                    session=runtime_module.SessionRef(
                        id="child-session",
                        parent_id="leader-session",
                    ),
                    status="completed",
                    turn=1,
                    metadata={},
                ),
                output="done",
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: StubRuntime())
    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": "child task",
                "parent_session_id": "leader-session",
            }
        ).encode("utf-8"),
    )
    payloads = _parse_sse_payloads(response)

    assert response.status == 200
    assert len(payloads) == 1
    assert cast(dict[str, object], payloads[0]["session"])["session"] == {
        "id": "child-session",
        "parent_id": "leader-session",
    }
    assert cast(dict[str, object], payloads[0]["session"])["status"] == "completed"
    assert payloads[0]["output"] == "done"


def test_transport_serializes_hook_events_from_runtime_stream(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    session = session_state(
        session=session_ref(id="hook-stream-session"),
        status="running",
        turn=1,
        metadata={"workspace": str(tmp_path)},
    )
    completed_session = session_state(
        session=session_ref(id="hook-stream-session"),
        status="completed",
        turn=1,
        metadata={"workspace": str(tmp_path)},
    )

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            command = _cwd_command()
            assert request.prompt == f"run {command}"
            yield runtime_stream_chunk(
                kind="event",
                session=session,
                event=event_envelope(
                    session_id="hook-stream-session",
                    sequence=1,
                    event_type="runtime.tool_hook_pre",
                    source="runtime",
                    payload={
                        "phase": "pre",
                        "tool_name": "shell_exec",
                        "session_id": "hook-stream-session",
                        "status": "ok",
                    },
                ),
            )
            yield runtime_stream_chunk(
                kind="event",
                session=completed_session,
                event=event_envelope(
                    session_id="hook-stream-session",
                    sequence=2,
                    event_type="runtime.tool_hook_post",
                    source="runtime",
                    payload={
                        "phase": "post",
                        "tool_name": "shell_exec",
                        "session_id": "hook-stream-session",
                        "status": "ok",
                    },
                ),
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: StubRuntime())
    command = _cwd_command()
    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": f"run {command}"}).encode("utf-8"),
    )
    payloads = _parse_sse_payloads(response)

    assert response.status == 200
    assert [cast(dict[str, object], payload["event"])["event_type"] for payload in payloads] == [
        "runtime.tool_hook_pre",
        "runtime.tool_hook_post",
    ]


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
    assert cast(dict[str, object], payload["session"])["session"] == {"id": "deny-session"}
    assert cast(dict[str, object], payload["session"])["status"] == "failed"
    assert cast(dict[str, object], payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert payload["output"] is None
    assert [event["event_type"] for event in cast(list[dict[str, object]], payload["events"])] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert (tmp_path / "danger.txt").exists() is False


def test_transport_resumes_multi_step_loop_and_persists_replay_over_http(tmp_path: Path) -> None:
    _ = (tmp_path / "source.txt").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)
    waiting_response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": _multi_step_prompt(),
                "session_id": "http-loop-session",
            }
        ).encode("utf-8"),
    )
    waiting_payloads = _parse_sse_payloads(waiting_response)
    approval_request_id = cast(
        str,
        cast(dict[str, object], cast(dict[str, object], waiting_payloads[-1]["event"])["payload"])[
            "request_id"
        ],
    )
    approve_response = _run_app(
        app,
        method="POST",
        path="/api/sessions/http-loop-session/approval",
        body=json.dumps(
            {
                "request_id": approval_request_id,
                "decision": "allow",
            }
        ).encode("utf-8"),
    )
    approve_payload = cast(dict[str, object], approve_response.json())
    list_response = _run_app(app, method="GET", path="/api/sessions")
    replay_response = _run_app(app, method="GET", path="/api/sessions/http-loop-session")
    replay_payload = cast(dict[str, object], replay_response.json())

    assert waiting_response.status == 200
    assert [payload["kind"] for payload in waiting_payloads] == ["event"] * 14
    assert [
        cast(dict[str, object], payload["event"])["event_type"] for payload in waiting_payloads
    ] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["session"] == {
        "id": "http-loop-session"
    }
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["status"] == "waiting"
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], waiting_payloads[-1]["session"])["metadata"],
        workspace=tmp_path,
    )

    assert approve_response.status == 200
    assert cast(dict[str, object], approve_payload["session"])["session"] == {
        "id": "http-loop-session"
    }
    assert cast(dict[str, object], approve_payload["session"])["status"] == "completed"
    assert cast(dict[str, object], approve_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], approve_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert approve_payload["output"] == (
        "Found 1 match(es) for 'copied' in copied.txt\ncopied.txt:1: copied marker"
    )
    assert [
        event["event_type"] for event in cast(list[dict[str, object]], approve_payload["events"])
    ] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert [
        event["sequence"] for event in cast(list[dict[str, object]], approve_payload["events"])
    ] == list(range(1, 31))
    assert list_response.status == 200
    assert list_response.json() == [
        {
            "session": {"id": "http-loop-session"},
            "status": "completed",
            "turn": 1,
            "prompt": _multi_step_prompt(),
            "updated_at": 2,
        }
    ]
    assert replay_response.status == 200
    assert replay_payload == approve_payload
    assert (tmp_path / "copied.txt").read_text(encoding="utf-8") == "copied marker"


def test_transport_denied_multi_step_loop_preserves_failed_replay_over_http(tmp_path: Path) -> None:
    _ = (tmp_path / "source.txt").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)
    waiting_response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": _multi_step_prompt(),
                "session_id": "http-deny-loop-session",
            }
        ).encode("utf-8"),
    )
    waiting_payloads = _parse_sse_payloads(waiting_response)
    approval_request_id = cast(
        str,
        cast(dict[str, object], cast(dict[str, object], waiting_payloads[-1]["event"])["payload"])[
            "request_id"
        ],
    )
    deny_response = _run_app(
        app,
        method="POST",
        path="/api/sessions/http-deny-loop-session/approval",
        body=json.dumps(
            {
                "request_id": approval_request_id,
                "decision": "deny",
            }
        ).encode("utf-8"),
    )
    deny_payload = cast(dict[str, object], deny_response.json())
    list_response = _run_app(app, method="GET", path="/api/sessions")
    replay_response = _run_app(app, method="GET", path="/api/sessions/http-deny-loop-session")
    replay_payload = cast(dict[str, object], replay_response.json())

    assert waiting_response.status == 200
    assert [payload["kind"] for payload in waiting_payloads] == ["event"] * 14
    assert cast(dict[str, object], waiting_payloads[-1]["event"])["event_type"] == (
        "runtime.approval_requested"
    )
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["session"] == {
        "id": "http-deny-loop-session"
    }
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["status"] == "waiting"
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], waiting_payloads[-1]["session"])["metadata"],
        workspace=tmp_path,
    )

    assert deny_response.status == 200
    assert cast(dict[str, object], deny_payload["session"])["session"] == {
        "id": "http-deny-loop-session"
    }
    assert cast(dict[str, object], deny_payload["session"])["status"] == "failed"
    assert cast(dict[str, object], deny_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], deny_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert deny_payload["output"] is None
    assert [
        event["event_type"] for event in cast(list[dict[str, object]], deny_payload["events"])
    ] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert [
        event["sequence"] for event in cast(list[dict[str, object]], deny_payload["events"])
    ] == list(range(1, 21))
    assert list_response.status == 200
    assert list_response.json() == [
        {
            "session": {"id": "http-deny-loop-session"},
            "status": "failed",
            "turn": 1,
            "prompt": _multi_step_prompt(),
            "updated_at": 2,
        }
    ]
    assert replay_response.status == 200
    assert replay_payload == deny_payload
    assert (tmp_path / "copied.txt").exists() is False


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


def test_transport_rejects_invalid_question_answer_payload(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/question-session/question",
        body=json.dumps({"request_id": "question-1", "responses": []}).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json() == {"error": "responses must be a non-empty array"}


def test_transport_rejects_invalid_question_answer_item_payload(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/question-session/question",
        body=json.dumps(
            {
                "request_id": "question-1",
                "responses": [
                    {
                        "header": "Runtime path",
                        "answers": [""],
                    }
                ],
            }
        ).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json() == {"error": "responses[0].answers[0] must be a non-empty string"}


def test_transport_rejects_non_post_method_for_question_route(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(app, method="GET", path="/api/sessions/question-session/question")

    assert response.status == 405
    assert response.json() == {"error": "method not allowed"}


def test_transport_answers_pending_question_over_http(tmp_path: Path) -> None:
    runtime_module = importlib.import_module("voidcode.runtime")
    create_runtime_app = _load_transport_app_factory()

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

        def session_result(self, *, session_id: str) -> object:
            raise AssertionError(f"session_result should not be called: {session_id}")

        def list_notifications(self) -> tuple[object, ...]:
            raise AssertionError("list_notifications should not be called")

        def acknowledge_notification(self, *, notification_id: str) -> object:
            raise AssertionError(
                f"acknowledge_notification should not be called: {notification_id}"
            )

        def resume(self, session_id: str, **_: object) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

        def answer_question(
            self,
            session_id: str,
            *,
            question_request_id: str,
            responses: tuple[object, ...],
        ) -> RuntimeResponseLike:
            assert session_id == "question-session"
            assert question_request_id == "question-1"
            assert len(responses) == 1
            response = cast(QuestionResponseLike, responses[0])
            assert response.header == "Runtime path"
            assert response.answers == ("Reuse existing",)
            return runtime_module.RuntimeResponse(
                session=runtime_module.SessionState(
                    session=runtime_module.SessionRef(id="question-session"),
                    status="completed",
                    turn=1,
                    metadata={"workspace": str(tmp_path)},
                ),
                events=(
                    runtime_module.EventEnvelope(
                        session_id="question-session",
                        sequence=1,
                        event_type="runtime.question_answered",
                        source="runtime",
                        payload={"request_id": "question-1"},
                    ),
                ),
                output="done",
            )

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: StubRuntime())
    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/question-session/question",
        body=json.dumps(
            {
                "request_id": "question-1",
                "responses": [
                    {"header": "Runtime path", "answers": ["Reuse existing"]},
                ],
            }
        ).encode("utf-8"),
    )
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert cast(dict[str, object], payload["session"])["session"] == {"id": "question-session"}
    assert cast(dict[str, object], payload["session"])["status"] == "completed"
    assert payload["output"] == "done"


def test_transport_returns_not_found_for_missing_pending_question(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    contracts_module = importlib.import_module("voidcode.runtime.contracts")

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

        def session_result(self, *, session_id: str) -> object:
            raise AssertionError(f"session_result should not be called: {session_id}")

        def list_notifications(self) -> tuple[object, ...]:
            raise AssertionError("list_notifications should not be called")

        def acknowledge_notification(self, *, notification_id: str) -> object:
            raise AssertionError(
                f"acknowledge_notification should not be called: {notification_id}"
            )

        def resume(self, session_id: str, **_: object) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

        def answer_question(
            self,
            session_id: str,
            *,
            question_request_id: str,
            responses: tuple[object, ...],
        ) -> RuntimeResponseLike:
            _ = session_id, question_request_id, responses
            raise contracts_module.NoPendingQuestionError(
                "no pending question for session: question-session"
            )

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: StubRuntime())
    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/question-session/question",
        body=json.dumps(
            {
                "request_id": "question-1",
                "responses": [
                    {"header": "Runtime path", "answers": ["Reuse existing"]},
                ],
            }
        ).encode("utf-8"),
    )

    assert response.status == 404
    assert response.json() == {"error": "no pending question for session: question-session"}


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
            assert request.metadata == {"provider_stream": True}
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

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

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
                "metadata": {"provider_stream": True},
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


def test_transport_run_stream_accepts_metadata_passthrough_for_skills_and_max_steps() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    session = session_state(
        session=session_ref(id="stream-meta-session"),
        status="completed",
        turn=1,
        metadata={"workspace": "/tmp/workspace"},
    )

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "transport meta"
            assert request.session_id == "stream-meta-session"
            assert request.metadata == {
                "provider_stream": True,
                "max_steps": 6,
                "skills": ["demo"],
            }
            yield runtime_stream_chunk(
                kind="event",
                session=session,
                event=event_envelope(
                    session_id="stream-meta-session",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": request.prompt},
                ),
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

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
                "prompt": "transport meta",
                "session_id": "stream-meta-session",
                "metadata": {"provider_stream": True, "max_steps": 6, "skills": ["demo"]},
            }
        ).encode("utf-8"),
    )

    assert response.status == 200


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

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

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
    assert cast(dict[str, object], replay_payload["session"])["session"] == {
        "id": "streamed-session"
    }
    assert cast(dict[str, object], replay_payload["session"])["status"] == "completed"
    assert cast(dict[str, object], replay_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], replay_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert replay_payload["output"] == "stream replay"
    assert [
        event["event_type"] for event in cast(list[dict[str, object]], replay_payload["events"])
    ] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
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
    assert cast(dict[str, object], first_replay_payload["session"])["session"] == {
        "id": first_session_id
    }
    assert cast(dict[str, object], first_replay_payload["session"])["status"] == "completed"
    assert cast(dict[str, object], first_replay_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], first_replay_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert cast(dict[str, object], second_replay_payload["session"])["session"] == {
        "id": second_session_id
    }
    assert cast(dict[str, object], second_replay_payload["session"])["status"] == "completed"
    assert cast(dict[str, object], second_replay_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], second_replay_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert first_replay_payload["output"] == "anonymous stream"
    assert second_replay_payload["output"] == "anonymous stream"


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
        "sequence": 9,
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
    assert cast(dict[str, object], replay_payload["session"])["session"] == {
        "id": "failed-stream-session"
    }
    assert cast(dict[str, object], replay_payload["session"])["status"] == "failed"
    assert cast(dict[str, object], replay_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], replay_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert replay_payload["output"] is None
    assert [
        event["event_type"] for event in cast(list[dict[str, object]], replay_payload["events"])
    ] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.failed",
    ]


def test_transport_serializes_structured_provider_failure_payloads() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    failed_session = session_state(
        session=session_ref(id="provider-failed-session"),
        status="failed",
        turn=1,
        metadata={"workspace": "/tmp/workspace"},
    )

    class FailingStubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "fail provider"
            yield runtime_stream_chunk(
                kind="event",
                session=failed_session,
                event=event_envelope(
                    session_id="provider-failed-session",
                    sequence=1,
                    event_type="runtime.failed",
                    source="runtime",
                    payload={
                        "error": "context exceeded",
                        "provider_error_kind": "context_limit",
                        "provider": "opencode",
                        "model": "gpt-5.4",
                    },
                ),
            )

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
        body=json.dumps({"prompt": "fail provider"}).encode("utf-8"),
    )
    payloads = _parse_sse_payloads(response)
    first_payload = payloads[0]
    first_event = cast(dict[str, object], first_payload["event"])

    assert response.status == 200
    assert first_event["payload"] == {
        "error": "context exceeded",
        "provider_error_kind": "context_limit",
        "provider": "opencode",
        "model": "gpt-5.4",
    }


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

    assert response.status == 500
    assert response.json() == {"error": "internal server error"}
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


def test_transport_rejects_unsupported_request_metadata_field() -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=Path("/tmp/workspace"))

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": "transport me",
                "metadata": {"client": "transport-test"},
            }
        ).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json() == {"error": "unsupported request metadata field(s): client"}


def test_transport_rejects_unknown_parent_session_in_run_stream_payload(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": "child task",
                "parent_session_id": "missing-parent",
            }
        ).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json() == {"error": "parent session does not exist: missing-parent"}


def test_transport_allows_parent_session_while_parent_stream_request_is_active(
    tmp_path: Path,
) -> None:
    _, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    service_module = importlib.import_module("voidcode.runtime.service")
    active_registry = service_module._ACTIVE_SESSION_REGISTRY

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(workspace=tmp_path),
    )

    parent_started = threading.Event()
    allow_parent_to_finish = threading.Event()

    original_register = active_registry.register

    def _register_and_signal(*, workspace: Path, session_id: str) -> None:
        original_register(workspace=workspace, session_id=session_id)
        if session_id == "leader-session":
            parent_started.set()
            allow_parent_to_finish.wait(timeout=5)

    parent_response_holder: dict[str, object] = {}

    def _run_parent() -> None:
        parent_response_holder["response"] = _run_app(
            app,
            method="POST",
            path="/api/runtime/run/stream",
            body=json.dumps(
                {
                    "prompt": "leader",
                    "session_id": "leader-session",
                }
            ).encode("utf-8"),
        )

    parent_thread = threading.Thread(target=_run_parent, daemon=True)

    with patch.object(active_registry, "register", _register_and_signal):
        parent_thread.start()
        assert parent_started.wait(timeout=5)
        child_response = _run_app(
            app,
            method="POST",
            path="/api/runtime/run/stream",
            body=json.dumps(
                {
                    "prompt": "child",
                    "parent_session_id": "leader-session",
                }
            ).encode("utf-8"),
        )
        allow_parent_to_finish.set()
        parent_thread.join(timeout=5)

    child_payloads = _parse_sse_payloads(child_response)
    first_payload = child_payloads[0]
    first_session = cast(dict[str, object], first_payload["session"])
    first_session_ref = cast(dict[str, object], first_session["session"])

    assert child_response.status == 200
    assert first_session_ref["parent_id"] == "leader-session"


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
