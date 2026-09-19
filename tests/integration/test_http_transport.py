from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast
from unittest.mock import patch

import pytest

from voidcode.runtime.contracts import RuntimeNotification

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    config_module = importlib.import_module("voidcode.runtime.config")
    monkeypatch.setattr(
        config_module,
        "_default_runtime_mcp_config",
        lambda: config_module.RuntimeMcpConfig(enabled=False),
    )
    monkeypatch.setattr(config_module, "_default_runtime_mcp_servers", lambda: {})
    http_module = importlib.import_module("voidcode.runtime.transport.http")

    async def _direct_stream(_self: object, runtime: object, request: object) -> Any:
        for chunk in cast(Any, runtime).run_stream(request):
            yield chunk

    monkeypatch.setattr(http_module.RuntimeTransportApp, "_stream_runtime_chunks", _direct_stream)


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
    event: object | None
    output: str | None


class RuntimeResponseLike(Protocol):
    session: SessionLike
    events: tuple[object, ...]
    output: str | None


class RuntimeSessionDebugSnapshotLike(Protocol):
    prompt: str


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

    def session_debug_snapshot(self, *, session_id: str) -> RuntimeSessionDebugSnapshotLike: ...


class RuntimeFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        tool_registry: object | None = None,
        graph: object | None = None,
        mcp_manager: object | None = None,
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
        config: object | None = None,
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


def _error_body(message: str, *, code: str | None = None) -> dict[str, object]:
    """The transport's error envelope: a message plus the runtime's optional code."""
    return {"error": message, "code": code}


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
    query_string: bytes = b"",
) -> _TransportResponse:
    messages: list[dict[str, object]] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
    }
    asyncio.run(app(scope, _receive, _send))

    start_message = next(message for message in sent if cast(str, message["type"]) == "http.response.start")
    headers = {key.decode("utf-8").lower(): value.decode("utf-8") for key, value in cast(list[tuple[bytes, bytes]], start_message["headers"])}
    body_parts = [cast(bytes, message.get("body", b"")) for message in sent if cast(str, message["type"]) == "http.response.body"]
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


def _event_types_from_payload_events(payload: dict[str, object], key: str = "events") -> list[str]:
    return [cast(str, event["event_type"]) for event in cast(list[dict[str, object]], payload[key])]


def _event_types_from_sse_payloads(payloads: list[dict[str, object]]) -> list[str]:
    event_types: list[str] = []
    for payload in payloads:
        event = payload.get("event")
        if isinstance(event, dict):
            event_types.append(cast(str, cast(dict[str, object], event)["event_type"]))
    return event_types


def _assert_ordered_event_types(actual: list[str], expected: list[str]) -> None:
    remaining = iter(actual)
    for expected_type in expected:
        for event_type in remaining:
            if event_type == expected_type:
                break
        else:
            raise AssertionError(f"missing ordered event type: {expected_type}; actual={actual}")


def _event_by_type(
    events: list[dict[str, object]],
    event_type: str,
    *,
    reverse: bool = False,
) -> dict[str, object]:
    source = reversed(events) if reverse else iter(events)
    for event in source:
        if event["event_type"] == event_type:
            return event
    raise AssertionError(f"missing event type: {event_type}")


def _sse_event_by_type(
    payloads: list[dict[str, object]],
    event_type: str,
    *,
    reverse: bool = False,
) -> dict[str, object]:
    source = reversed(payloads) if reverse else iter(payloads)
    for payload in source:
        event = payload.get("event")
        if isinstance(event, dict):
            typed_event = cast(dict[str, object], event)
            if typed_event.get("event_type") == event_type:
                return typed_event
    raise AssertionError(f"missing SSE event type: {event_type}")


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


def test_transport_updates_runtime_web_settings_and_hides_api_key_on_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert update_payload["provider"] == "opencode-go"
    assert update_payload["provider_api_key_present"] is True
    assert update_payload["model"] == "opencode-go/glm-5.1"
    assert read_response.status == 200
    assert read_payload["provider"] == update_payload["provider"]
    assert read_payload["provider_api_key_present"] is update_payload["provider_api_key_present"]
    assert read_payload["model"] == update_payload["model"]


def test_transport_handles_lifespan_startup_and_shutdown(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    sent = _run_lifespan(app)

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


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


def test_transport_replays_session_as_json_runtime_response(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("http replay\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()

    runtime = runtime_class(workspace=tmp_path)
    stored = runtime.run(runtime_request(prompt="read sample.txt", session_id="transport-session"))
    stored_runtime_state = cast(dict[str, object], cast(dict[str, object], stored.session.metadata["runtime_state"]))
    assert "pending_tool_intent" not in stored_runtime_state

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(app, method="GET", path="/api/sessions/transport-session")
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert payload["output"] == "Read 1 line(s) from sample.txt."
    request_event = _event_by_type(
        cast(list[dict[str, object]], payload["events"]),
        "runtime.request_received",
    )
    request_payload = cast(dict[str, object], request_event["payload"])
    policy_observability = cast(dict[str, object], request_payload["runtime_policy"])
    assert policy_observability["bounded"] is True
    assert policy_observability["redacted"] is True
    assert "http replay" not in json.dumps(policy_observability)
    replay_session = cast(dict[str, object], payload["session"])
    expected_metadata = dict(stored.session.metadata)
    expected_metadata.pop("prompt_stack", None)
    expected_metadata.pop("provider_context", None)
    expected_metadata.pop("_prompt_activation_this_run", None)
    assert replay_session["session"] == {"id": "transport-session"}
    assert replay_session["status"] == stored.session.status
    assert replay_session["turn"] == stored.session.turn
    replay_metadata = cast(dict[str, object], replay_session["metadata"])
    replay_runtime_policy = cast(dict[str, object], replay_metadata.pop("runtime_policy"))
    expected_runtime_policy = cast(dict[str, object], expected_metadata.pop("runtime_policy"))
    assert replay_metadata == expected_metadata
    assert replay_runtime_policy["prompt_activation"] == {
        **cast(dict[str, object], expected_runtime_policy["prompt_activation"]),
        "activated_this_turn": False,
    }
    _assert_ordered_event_types(
        _event_types_from_payload_events(payload),
        [
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
        ],
    )


def test_transport_get_session_replay_is_read_only_for_interrupted_session(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("replay read only\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()

    runtime = runtime_class(workspace=tmp_path)
    stored = runtime.run(runtime_request(prompt="read sample.txt", session_id="interrupted-replay-session"))
    original_events = cast(Any, stored).events
    original_count = len(original_events)
    store = runtime._session_store
    loaded = store.load_session(workspace=tmp_path, session_id="interrupted-replay-session")
    # Un-seal the terminal row into the mid-run "interrupted" state (the same
    # state every session row has while it is running) with an interrupted
    # checkpoint at the end of the persisted transcript.
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="interrupted-replay-session",
        prompt="read sample.txt",
        session_metadata=loaded.session.metadata,
        tool_results=(),
        last_event_sequence=original_count,
    )
    interrupted = store.load_session(workspace=tmp_path, session_id="interrupted-replay-session")
    assert interrupted.session.status == "interrupted"

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(app, method="GET", path="/api/sessions/interrupted-replay-session")
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert cast(dict[str, object], payload["session"])["status"] == "interrupted"
    replay_events = cast(list[dict[str, object]], payload["events"])
    assert len(replay_events) == original_count
    assert sum(1 for event in replay_events if event["event_type"] == "graph.model_turn") == 1
    # The persisted transcript must be untouched: no truncation, no re-run, no
    # new provider turn appended.
    after = store.load_session(workspace=tmp_path, session_id="interrupted-replay-session")
    assert len(after.events) == original_count
    assert [event.sequence for event in after.events] == [event.sequence for event in original_events]
    assert [event.event_type for event in after.events] == [event.event_type for event in original_events]


def test_transport_post_session_resume_reexecutes_interrupted_session(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("resume reexecutes\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()

    runtime = runtime_class(workspace=tmp_path)
    stored = runtime.run(runtime_request(prompt="read sample.txt", session_id="resume-session"))
    original_count = len(cast(Any, stored).events)
    store = runtime._session_store
    loaded = store.load_session(workspace=tmp_path, session_id="resume-session")
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id="resume-session",
        prompt="read sample.txt",
        session_metadata=loaded.session.metadata,
        tool_results=(),
        last_event_sequence=original_count,
    )

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(app, method="POST", path="/api/sessions/resume-session/resume")
    payload = cast(dict[str, object], response.json())

    assert response.status == 200
    assert cast(dict[str, object], payload["session"])["status"] == "completed"
    # The explicit resume re-executes the graph loop from the checkpoint: new
    # events are appended past the original transcript and the row is sealed.
    after = store.load_session(workspace=tmp_path, session_id="resume-session")
    assert len(after.events) > original_count
    assert [event.sequence for event in after.events] == list(range(1, len(after.events) + 1))
    assert after.events[-1].event_type == "graph.response_ready"
    assert sum(1 for event in after.events if event.event_type == "graph.model_turn") >= 2
    # GET after the explicit resume is again read-only replay.
    replay_response = _run_app(
        create_runtime_app(workspace=tmp_path),
        method="GET",
        path="/api/sessions/resume-session",
    )
    replay_payload = cast(dict[str, object], replay_response.json())
    assert replay_response.status == 200
    assert cast(dict[str, object], replay_payload["session"])["status"] == "completed"
    assert len(cast(list[dict[str, object]], replay_payload["events"])) == len(after.events)
    replay_after = store.load_session(workspace=tmp_path, session_id="resume-session")
    assert len(replay_after.events) == len(after.events)


def test_transport_streams_session_events_after_sequence(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("events payload\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    runtime = runtime_class(workspace=tmp_path)
    stored = runtime.run(runtime_request(prompt="read sample.txt", session_id="events-session"))

    response = _run_app(
        create_runtime_app(workspace=tmp_path),
        method="GET",
        path="/api/sessions/events-session/events",
        query_string=b"after_sequence=1",
    )

    assert response.status == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    chunks = [json.loads(part.removeprefix(b"data: ").strip()) for part in response.body_parts if part.startswith(b"data: ")]
    assert chunks[0]["kind"] == "session"
    assert chunks[0]["session"]["session"]["id"] == "events-session"
    event_chunks = [chunk for chunk in chunks if chunk["kind"] == "event"]
    assert [chunk["event"]["sequence"] for chunk in event_chunks] == [
        cast(Any, event).sequence for event in stored.events if cast(Any, event).sequence > 1
    ]
    assert all(chunk["session"] is None for chunk in event_chunks)


def test_transport_session_result_redacts_reasoning_until_query_opt_in() -> None:
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    runtime_contracts = importlib.import_module("voidcode.runtime.contracts")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")

    session = runtime_session.SessionState(
        session=runtime_session.SessionRef(id="reasoning-session"),
        status="completed",
        metadata={"show_thinking": True},
    )
    reasoning_event = runtime_events.EventEnvelope(
        session_id="reasoning-session",
        sequence=1,
        event_type="runtime.reasoning_part",
        source="runtime",
        payload=runtime_events.runtime_reasoning_part_payload(text="private chain"),
    )
    result = runtime_contracts.RuntimeSessionResult(
        session=session,
        prompt="think",
        status="completed",
        summary="Completed",
        output="answer",
        transcript=(reasoning_event,),
        last_event_sequence=1,
    )

    class ReasoningResultRuntime:
        def session_result(self, *, session_id: str) -> object:
            assert session_id == "reasoning-session"
            return result

        def acknowledge_notification(self, *, notification_id: str) -> RuntimeNotification:
            raise AssertionError(f"acknowledge_notification should not be called: {notification_id}")

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, ReasoningResultRuntime))

    redacted_response = _run_app(
        app,
        method="GET",
        path="/api/sessions/reasoning-session/result",
    )
    redacted_payload = cast(dict[str, object], redacted_response.json())
    redacted_event = cast(list[dict[str, object]], redacted_payload["transcript"])[0]
    redacted_reasoning = cast(dict[str, object], redacted_event["payload"])
    assert redacted_reasoning["text_omitted"] is True
    assert redacted_reasoning["preview_omitted"] is True
    assert "private chain" not in json.dumps(redacted_payload)

    shown_response = _run_app(
        app,
        method="GET",
        path="/api/sessions/reasoning-session/result",
        query_string=b"show_thinking=true",
    )
    shown_payload = cast(dict[str, object], shown_response.json())
    shown_event = cast(list[dict[str, object]], shown_payload["transcript"])[0]
    shown_reasoning = cast(dict[str, object], shown_event["payload"])
    assert shown_reasoning["text"] == "private chain"
    assert shown_reasoning["preview"] == "private chain"


def test_transport_replay_response_redacts_reasoning_until_query_opt_in() -> None:
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    runtime_contracts = importlib.import_module("voidcode.runtime.contracts")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")

    session = runtime_session.SessionState(
        session=runtime_session.SessionRef(id="reasoning-session"),
        status="completed",
        metadata={"show_thinking": True},
    )
    reasoning_event = runtime_events.EventEnvelope(
        session_id="reasoning-session",
        sequence=1,
        event_type="runtime.reasoning_part",
        source="runtime",
        payload=runtime_events.runtime_reasoning_part_payload(text="private chain"),
    )
    response = runtime_contracts.RuntimeResponse(
        session=session,
        events=(reasoning_event,),
        output="answer",
    )

    class ReasoningResumeRuntime:
        def replay_session(self, *, session_id: str) -> object:
            assert session_id == "reasoning-session"
            return response

        def acknowledge_notification(self, *, notification_id: str) -> RuntimeNotification:
            raise AssertionError(f"acknowledge_notification should not be called: {notification_id}")

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, ReasoningResumeRuntime))

    redacted_response = _run_app(
        app,
        method="GET",
        path="/api/sessions/reasoning-session",
    )
    redacted_payload = cast(dict[str, object], redacted_response.json())
    redacted_event = cast(list[dict[str, object]], redacted_payload["events"])[0]
    redacted_reasoning = cast(dict[str, object], redacted_event["payload"])
    assert redacted_reasoning["text_omitted"] is True
    assert redacted_reasoning["preview_omitted"] is True
    assert "private chain" not in json.dumps(redacted_payload)

    shown_response = _run_app(
        app,
        method="GET",
        path="/api/sessions/reasoning-session",
        query_string=b"show_thinking=true",
    )
    shown_payload = cast(dict[str, object], shown_response.json())
    shown_event = cast(list[dict[str, object]], shown_payload["events"])[0]
    shown_reasoning = cast(dict[str, object], shown_event["payload"])
    assert shown_reasoning["text"] == "private chain"
    assert shown_reasoning["preview"] == "private chain"


def test_transport_background_task_output_redacts_reasoning_until_query_opt_in() -> None:
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    runtime_contracts = importlib.import_module("voidcode.runtime.contracts")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")

    task_result = runtime_contracts.BackgroundTaskResult(
        task_id="task-reasoning",
        parent_session_id="parent-session",
        child_session_id="child-session",
        status="completed",
        summary_output="answer",
        result_available=True,
    )
    session = runtime_session.SessionState(
        session=runtime_session.SessionRef(id="child-session"),
        status="completed",
        metadata={"show_thinking": True},
    )
    reasoning_event = runtime_events.EventEnvelope(
        session_id="child-session",
        sequence=1,
        event_type="runtime.reasoning_part",
        source="runtime",
        payload=runtime_events.runtime_reasoning_part_payload(text="private chain"),
    )
    session_result = runtime_contracts.RuntimeSessionResult(
        session=session,
        prompt="think",
        status="completed",
        summary="Completed",
        output="answer",
        transcript=(reasoning_event,),
        last_event_sequence=1,
    )

    class ReasoningTaskRuntime:
        def load_background_task_result(self, task_id: str) -> object:
            assert task_id == "task-reasoning"
            return task_result

        def session_result(self, *, session_id: str) -> object:
            assert session_id == "child-session"
            return session_result

        def acknowledge_notification(self, *, notification_id: str) -> RuntimeNotification:
            raise AssertionError(f"acknowledge_notification should not be called: {notification_id}")

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, ReasoningTaskRuntime))

    redacted_response = _run_app(
        app,
        method="GET",
        path="/api/tasks/task-reasoning/output",
    )
    redacted_payload = cast(dict[str, object], redacted_response.json())
    redacted_session = cast(dict[str, object], redacted_payload["session_result"])
    redacted_event = cast(list[dict[str, object]], redacted_session["transcript"])[0]
    redacted_reasoning = cast(dict[str, object], redacted_event["payload"])
    assert redacted_reasoning["text_omitted"] is True
    assert redacted_reasoning["preview_omitted"] is True
    assert "private chain" not in json.dumps(redacted_payload)

    shown_response = _run_app(
        app,
        method="GET",
        path="/api/tasks/task-reasoning/output",
        query_string=b"show_thinking=true",
    )
    shown_payload = cast(dict[str, object], shown_response.json())
    shown_session = cast(dict[str, object], shown_payload["session_result"])
    shown_event = cast(list[dict[str, object]], shown_session["transcript"])[0]
    shown_reasoning = cast(dict[str, object], shown_event["payload"])
    assert shown_reasoning["text"] == "private chain"
    assert shown_reasoning["preview"] == "private chain"


def test_transport_resolves_pending_approval_allow_over_http(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    waiting = runtime.run(runtime_request(prompt="write danger.txt approved later", session_id="approval-session"))
    approval_request_id = cast(str, cast(Any, waiting.events[-1]).payload["request_id"])

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
    assert payload["output"] == "Wrote file successfully: danger.txt"
    _assert_ordered_event_types(
        _event_types_from_payload_events(payload),
        [
            "runtime.request_received",
            "runtime.skills_loaded",
            "graph.loop_step",
            "graph.model_turn",
            "graph.tool_request_created",
            "runtime.tool_lookup_succeeded",
            "runtime.approval_requested",
            "runtime.approval_resolved",
            "runtime.tool_started",
            "runtime.tool_completed",
            "graph.loop_step",
            "graph.response_ready",
        ],
    )
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved later"


@pytest.mark.parametrize(
    ("method", "path", "expected_status", "expected_error"),
    (
        ("POST", "/api/notifications", 405, "method not allowed"),
        ("GET", "/api/notifications/missing-notification/ack", 405, "method not allowed"),
        ("POST", "/api/notifications/missing-notification/ack", 404, "unknown notification: missing-notification"),
    ),
)
def test_transport_notification_routes_enforce_methods_and_workspace_ownership(
    tmp_path: Path,
    method: str,
    path: str,
    expected_status: int,
    expected_error: str,
) -> None:
    """Notification reads and writes stay within the active workspace boundary."""
    create_runtime_app = _load_transport_app_factory()
    response = _run_app(
        create_runtime_app(workspace=tmp_path),
        method=method,
        path=path,
    )

    assert response.status == expected_status
    assert response.json() == _error_body(expected_error)


def test_transport_stream_preserves_tool_display_metadata(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    session = session_state(
        session=session_ref(id="tool-display-stream"),
        status="running",
        turn=1,
        metadata={"workspace": str(tmp_path)},
    )

    display: dict[str, object] = {
        "kind": "shell",
        "title": "Shell",
        "summary": "Run failing tests",
        "args": ["npm test"],
        "copyable": {"command": "npm test", "output": "stderr boom"},
    }

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "stream tool display metadata"
            yield runtime_stream_chunk(
                kind="event",
                session=session,
                event=event_envelope(
                    session_id="tool-display-stream",
                    sequence=1,
                    event_type="runtime.tool_completed",
                    source="runtime",
                    payload={
                        "tool": "shell_exec",
                        "tool_call_id": "shell-1",
                        "status": "error",
                        "arguments": {"command": "npm test"},
                        "error": "process failed",
                        "display": display,
                        "tool_status": {
                            "invocation_id": "shell-1",
                            "tool_name": "shell_exec",
                            "phase": "completed",
                            "status": "failed",
                            "display": display,
                        },
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
    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "stream tool display metadata"}).encode("utf-8"),
    )
    payloads = _parse_sse_payloads(response)
    event = cast(dict[str, object], payloads[0]["event"])
    event_payload = cast(dict[str, object], event["payload"])
    tool_status = cast(dict[str, object], event_payload["tool_status"])

    assert response.status == 200
    assert event_payload["display"] == display
    assert tool_status["display"] == display
    assert cast(dict[str, object], tool_status["display"])["copyable"] == {
        "command": "npm test",
        "output": "stderr boom",
    }


def test_transport_resolves_pending_approval_deny_over_http(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    waiting = runtime.run(runtime_request(prompt="write danger.txt denied later", session_id="deny-session"))
    approval_request_id = cast(str, cast(Any, waiting.events[-1]).payload["request_id"])

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
    assert cast(dict[str, object], payload["session"])["status"] == "running"
    assert cast(dict[str, object], payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert payload["output"] is None
    events = cast(list[dict[str, object]], payload["events"])
    _assert_ordered_event_types(
        _event_types_from_payload_events(payload),
        [
            "runtime.request_received",
            "runtime.skills_loaded",
            "graph.loop_step",
            "graph.model_turn",
            "graph.tool_request_created",
            "runtime.tool_lookup_succeeded",
            "runtime.approval_requested",
            "runtime.approval_resolved",
            "runtime.tool_completed",
        ],
    )
    feedback_payload = cast(dict[str, object], _event_by_type(events, "runtime.tool_completed", reverse=True)["payload"])
    assert feedback_payload["status"] == "error"
    assert feedback_payload["permission_denied"] is True
    assert feedback_payload["denied_by"] == "user"
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
        cast(dict[str, object], cast(dict[str, object], waiting_payloads[-1]["event"])["payload"])["request_id"],
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
    assert all(payload["kind"] == "event" for payload in waiting_payloads)
    _assert_ordered_event_types(
        _event_types_from_sse_payloads(waiting_payloads),
        [
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
        ],
    )
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["session"] == {"id": "http-loop-session"}
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["status"] == "waiting"
    assert cast(dict[str, object], waiting_payloads[-1]["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], waiting_payloads[-1]["session"])["metadata"],
        workspace=tmp_path,
    )

    assert approve_response.status == 200
    assert cast(dict[str, object], approve_payload["session"])["session"] == {"id": "http-loop-session"}
    assert cast(dict[str, object], approve_payload["session"])["status"] == "completed"
    assert cast(dict[str, object], approve_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], approve_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    approve_session_metadata = cast(dict[str, object], cast(dict[str, object], approve_payload["session"])["metadata"])
    approve_runtime_state = cast(dict[str, object], approve_session_metadata["runtime_state"])
    assert "pending_tool_intent" not in approve_runtime_state
    assert approve_payload["output"] == ("Found 1 match(es) for 'copied' in copied.txt\ncopied.txt:1: copied marker")
    approve_events = cast(list[dict[str, object]], approve_payload["events"])
    _assert_ordered_event_types(
        _event_types_from_payload_events(approve_payload),
        [
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
        ],
    )
    assert [event["sequence"] for event in approve_events] == list(range(1, cast(int, approve_events[-1]["sequence"]) + 1))
    assert list_response.status == 200
    listed_row = cast(dict[str, object], cast(list[object], list_response.json())[0])
    updated_at = listed_row.pop("updated_at")
    assert isinstance(updated_at, int) and updated_at >= 1
    assert listed_row == {
        "session": {"id": "http-loop-session"},
        "status": "completed",
        "turn": 1,
        "prompt": _multi_step_prompt(),
    }
    assert replay_response.status == 200
    replay_session = cast(dict[str, object], replay_payload["session"])
    approve_session = cast(dict[str, object], approve_payload["session"])
    replay_metadata = cast(dict[str, object], replay_session["metadata"])
    approve_metadata = cast(dict[str, object], approve_session["metadata"])
    assert replay_payload["output"] == approve_payload["output"]
    assert _event_types_from_payload_events(replay_payload) == _event_types_from_payload_events(approve_payload)
    assert replay_session["session"] == approve_session["session"]
    assert replay_session["status"] == approve_session["status"]
    assert replay_session["turn"] == approve_session["turn"]
    expected_metadata = dict(approve_metadata)
    expected_metadata.pop("prompt_stack", None)
    expected_metadata.pop("provider_context", None)
    expected_metadata.pop("_prompt_activation_this_run", None)
    assert replay_metadata == expected_metadata
    assert (tmp_path / "copied.txt").read_text(encoding="utf-8") == "copied marker"


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
    assert response.json() == _error_body(expected_error)


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

    app = create_runtime_app(workspace=Path("/tmp/workspace"), runtime_factory=lambda: StubRuntime())

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
    assert [cast(dict[str, object], payload["event"])["event_type"] for payload in payloads if payload["event"] is not None] == [
        "runtime.request_received",
        "graph.response_ready",
    ]
    assert payloads[-1]["output"] == "transported"


def test_transport_run_stream_cancels_run_on_client_disconnect() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state, event_envelope = _load_stream_types()
    session = session_state(
        session=session_ref(id="disconnect-session"),
        status="running",
        turn=1,
        metadata={"workspace": "/tmp/workspace"},
    )

    cancelled: list[tuple[str, str | None]] = []

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            for sequence in range(1, 30):
                yield runtime_stream_chunk(
                    kind="event",
                    session=session,
                    event=event_envelope(
                        session_id="disconnect-session",
                        sequence=sequence,
                        event_type="runtime.request_received",
                        source="runtime",
                        payload={"sequence": sequence},
                    ),
                )

        def cancel_session(
            self,
            session_id: str,
            *,
            run_id: str | None = None,
            reason: str | None = None,
        ) -> object:
            cancelled.append((session_id, reason))
            return SimpleNamespace(interrupted=False, as_payload=lambda: {})

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            raise AssertionError("list_sessions should not be called")

        def web_settings(self) -> dict[str, object]:
            raise AssertionError("web_settings should not be called")

        def update_web_settings(self, **_: object) -> dict[str, object]:
            raise AssertionError("update_web_settings should not be called")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp/workspace"), runtime_factory=lambda: StubRuntime())

    sent: list[dict[str, object]] = []
    messages: list[dict[str, object]] = [
        {
            "type": "http.request",
            "body": json.dumps(
                {
                    "prompt": "disconnect me",
                    "session_id": "disconnect-session",
                    "metadata": {"provider_stream": True},
                }
            ).encode("utf-8"),
            "more_body": False,
        }
    ]

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)
        # A real ASGI send suspends on the socket write. Yielding here is what
        # lets the server's disconnect listener deliver the dropped connection
        # to the streaming task instead of the response running to completion.
        await asyncio.sleep(0)

    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/api/runtime/run/stream",
        "query_string": b"",
    }
    asyncio.run(app(scope, _receive, _send))

    start_message = next(message for message in sent if cast(str, message["type"]) == "http.response.start")
    assert cast(int, start_message["status"]) == 200
    body_parts = [cast(bytes, message.get("body", b"")) for message in sent if cast(str, message["type"]) == "http.response.body"]
    data_parts = [part for part in body_parts if part.startswith(b"data: ")]
    assert len(data_parts) < 30
    assert cancelled == [("disconnect-session", "client_disconnected")]


def test_transport_session_events_stops_replay_burst_on_client_disconnect() -> None:
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")
    contracts_module = importlib.import_module("voidcode.runtime.contracts")

    replay_calls: list[int] = []

    class _DisconnectRuntime:
        def replay_session(self, *, session_id: str) -> object:
            replay_calls.append(len(replay_calls) + 1)
            events = tuple(
                runtime_events.EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"sequence": sequence},
                )
                for sequence in range(1, 100)
            )
            return contracts_module.RuntimeResponse(
                session=runtime_session.SessionState(
                    session=runtime_session.SessionRef(id=session_id),
                    status="running",
                    turn=1,
                    metadata={},
                ),
                events=events,
                output=None,
            )

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, _DisconnectRuntime))

    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)
        # A real ASGI send suspends on the socket write; yielding is what lets
        # the server's disconnect listener interrupt the replay burst.
        await asyncio.sleep(0)

    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions/disconnect-session/events",
        "query_string": b"after_sequence=0&follow=true",
    }
    asyncio.run(app(scope, _receive, _send))

    start_message = next(message for message in sent if cast(str, message["type"]) == "http.response.start")
    assert cast(int, start_message["status"]) == 200
    body_parts = [cast(bytes, message.get("body", b"")) for message in sent if cast(str, message["type"]) == "http.response.body"]
    data_parts = [part for part in body_parts if part.startswith(b"data: ")]
    # A 99-event replay must not be pushed into a disconnected socket: the
    # disconnect is observed within at most one chunk, and the follow loop must
    # not keep replaying on the dead connection.
    assert len(data_parts) <= 2
    assert len(replay_calls) == 1


def test_transport_session_events_follow_closes_on_interrupted_session() -> None:
    """Follow stream must close on an ``interrupted`` session.

    A user-cancelled run now seals ``interrupted`` (the terminal-status
    derivation keys off the ``cancelled`` flag), so the session-event follow
    stream must close on ``interrupted`` exactly like ``completed``/``failed``
    instead of polling the replayed snapshot forever.
    """
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    runtime_session = importlib.import_module("voidcode.runtime.session")
    contracts_module = importlib.import_module("voidcode.runtime.contracts")

    replay_calls: list[int] = []

    class _InterruptedRuntime:
        def replay_session(self, *, session_id: str) -> object:
            replay_calls.append(len(replay_calls) + 1)
            return contracts_module.RuntimeResponse(
                session=runtime_session.SessionState(
                    session=runtime_session.SessionRef(id=session_id),
                    status="interrupted",
                    turn=1,
                    metadata={},
                ),
                events=(),
                output=None,
            )

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, _InterruptedRuntime))

    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        await asyncio.sleep(3600)  # half-open socket: a polling follow loop would hang here

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions/interrupted-follow-session/events",
        "query_string": b"after_sequence=0&follow=true",
    }
    # Terminates promptly (the interrupted status closes the follow loop)
    # instead of polling the snapshot forever.
    asyncio.run(asyncio.wait_for(app(scope, _receive, _send), timeout=5.0))

    assert replay_calls == [1]
    start_message = next(message for message in sent if cast(str, message["type"]) == "http.response.start")
    assert cast(int, start_message["status"]) == 200


def test_transport_session_events_stops_cleanly_when_send_raises_during_replay() -> None:
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")
    contracts_module = importlib.import_module("voidcode.runtime.contracts")

    class _DeadSocketRuntime:
        def replay_session(self, *, session_id: str) -> object:
            events = tuple(
                runtime_events.EventEnvelope(
                    session_id=session_id,
                    sequence=sequence,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"sequence": sequence},
                )
                for sequence in range(1, 100)
            )
            return contracts_module.RuntimeResponse(
                session=runtime_session.SessionState(
                    session=runtime_session.SessionRef(id=session_id),
                    status="running",
                    turn=1,
                    metadata={},
                ),
                events=events,
                output=None,
            )

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, _DeadSocketRuntime))

    sent: list[dict[str, object]] = []
    send_count = 0

    async def _receive() -> dict[str, object]:
        await asyncio.sleep(3600)  # half-open socket: no http.disconnect arrives

    async def _send(message: dict[str, object]) -> None:
        nonlocal send_count
        send_count += 1
        if send_count > 3:  # response.start + snapshot + first chunk, then the socket dies
            raise ConnectionResetError("socket closed by peer")
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions/disconnect-session/events",
        "query_string": b"after_sequence=0&follow=true",
    }
    # Must terminate cleanly (no exception propagates out of the app) once the
    # send starts failing, instead of replaying into the dead socket.
    asyncio.run(app(scope, _receive, _send))

    data_parts = [part for part in sent if cast(bytes, part.get("body", b"")).startswith(b"data: ")]
    assert len(data_parts) == 2  # snapshot + first chunk only


def test_transport_session_events_follow_reads_incrementally_after_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Follow ticks must read only the events past the client cursor.

    The full transcript is replayed once when the stream opens; every later
    tick asks the runtime for the events after the advancing cursor plus the
    session status, delivers each event exactly once in order, and closes on a
    terminal status.
    """
    runtime_transport = importlib.import_module("voidcode.runtime.transport.http")
    runtime_contracts = importlib.import_module("voidcode.runtime.contracts")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")

    # The follow stream reads the interval from its defining module.
    monkeypatch.setattr(runtime_transport, "_SESSION_EVENT_FOLLOW_POLL_SECONDS", 0.01)

    replay_calls: list[str] = []
    follow_cursors: list[int] = []

    def _event(session_id: str, sequence: int) -> object:
        return runtime_events.EventEnvelope(
            session_id=session_id,
            sequence=sequence,
            event_type="graph.provider_stream",
            source="graph",
            payload={"sequence": sequence},
        )

    class _IncrementalFollowRuntime:
        def replay_session(self, *, session_id: str) -> object:
            replay_calls.append(session_id)
            return runtime_contracts.RuntimeResponse(
                session=runtime_session.SessionState(
                    session=runtime_session.SessionRef(id=session_id),
                    status="running",
                    turn=1,
                    metadata={},
                ),
                events=(_event(session_id, 1), _event(session_id, 2)),
                output=None,
            )

        def session_events_after(self, *, session_id: str, after_sequence: int) -> object:
            follow_cursors.append(after_sequence)
            if after_sequence < 4:
                return runtime_contracts.SessionEventBatch(
                    status="running",
                    events=(_event(session_id, 3), _event(session_id, 4)),
                )
            return runtime_contracts.SessionEventBatch(status="completed", events=())

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_transport.RuntimeTransportApp(runtime_factory=cast(Any, _IncrementalFollowRuntime))

    response = _run_app(
        app,
        method="GET",
        path="/api/sessions/incremental-follow-session/events",
        query_string=b"after_sequence=0&follow=true",
    )

    assert response.status == 200
    payloads = _parse_sse_payloads(response)
    assert [payload["kind"] for payload in payloads] == ["session", "event", "event", "event", "event"]
    assert [cast(dict[str, object], payload["event"])["sequence"] for payload in payloads[1:]] == [1, 2, 3, 4]
    # One full replay when the stream opens; ticks never replay the transcript.
    assert replay_calls == ["incremental-follow-session"]
    # Each tick asks only for the events past the cursor, which advances
    # monotonically with the delivered batches (1/2 replayed, then 3/4).
    assert follow_cursors == [2, 4]


def test_transport_run_stream_sends_session_state_only_when_it_changes() -> None:
    """Run-stream frames must not re-send unchanged session state.

    The run stream emits one frame per provider delta; repeating the (tens of
    kilobytes) session metadata on every frame multiplied wire traffic and
    client parsing by the metadata size. The first frame of a response carries
    the full state, later frames carry ``null`` until the state really changes.
    """
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    runtime_contracts = importlib.import_module("voidcode.runtime.contracts")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")

    session_id = "session-state-session"
    metadata: dict[str, object] = {"context_window": {"tokens": 1024}, "pending_messages": []}
    running_session = runtime_session.SessionState(
        session=runtime_session.SessionRef(id=session_id),
        status="running",
        turn=1,
        metadata=metadata,
    )
    # Equal-but-distinct metadata must not re-emit the whole state.
    equal_session = runtime_session.SessionState(
        session=runtime_session.SessionRef(id=session_id),
        status="running",
        turn=1,
        metadata=dict(metadata),
    )
    # A later frame keeps the same object but updates its metadata in place.
    in_place_session = runtime_session.SessionState(
        session=runtime_session.SessionRef(id=session_id),
        status="running",
        turn=1,
        metadata={"context_window": {"tokens": 4096}},
    )
    completed_session = runtime_session.SessionState(
        session=runtime_session.SessionRef(id=session_id),
        status="completed",
        turn=2,
        metadata=metadata,
    )

    def _event(sequence: int) -> object:
        return runtime_events.EventEnvelope(
            session_id=session_id,
            sequence=sequence,
            event_type="graph.provider_stream",
            source="graph",
            payload={"sequence": sequence},
        )

    class _SessionStateRunRuntime:
        def run_stream(self, request: object) -> Iterator[object]:
            yield runtime_contracts.RuntimeStreamChunk(kind="event", session=running_session, event=_event(1))
            yield runtime_contracts.RuntimeStreamChunk(kind="event", session=running_session, event=_event(2))
            yield runtime_contracts.RuntimeStreamChunk(kind="event", session=equal_session, event=_event(3))
            yield runtime_contracts.RuntimeStreamChunk(kind="event", session=completed_session, event=_event(4))
            yield runtime_contracts.RuntimeStreamChunk(kind="event", session=in_place_session, event=_event(5))
            in_place_session.metadata["context_window"] = {"tokens": 8192}
            yield runtime_contracts.RuntimeStreamChunk(kind="event", session=in_place_session, event=_event(6))

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, _SessionStateRunRuntime))

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "stream state"}).encode("utf-8"),
    )

    assert response.status == 200
    payloads = _parse_sse_payloads(response)
    assert [cast(dict[str, object], payload["event"])["sequence"] for payload in payloads] == [1, 2, 3, 4, 5, 6]
    first_session = cast(dict[str, object], payloads[0]["session"])
    assert first_session["status"] == "running"
    assert first_session["metadata"] == {"context_window": {"tokens": 1024}, "pending_messages": []}
    assert payloads[1]["session"] is None
    assert payloads[2]["session"] is None
    changed_session = cast(dict[str, object], payloads[3]["session"])
    assert changed_session["status"] == "completed"
    assert changed_session["turn"] == 2
    assert changed_session["metadata"] == metadata
    # The in-place frame is a new state, so it is emitted in full ...
    assert cast(dict[str, object], payloads[4]["session"])["metadata"] == {"context_window": {"tokens": 4096}}
    # ... and a mutation of the same object re-emits instead of going stale.
    assert cast(dict[str, object], payloads[5]["session"])["metadata"] == {"context_window": {"tokens": 8192}}


def test_transport_run_stream_rejects_unknown_request_fields() -> None:
    create_runtime_app = _load_transport_app_factory()

    class _RuntimeMustNotStart:
        def __init__(self) -> None:
            raise AssertionError("runtime must not be constructed for invalid payload")

    app = create_runtime_app(workspace=Path("/tmp/workspace"), runtime_factory=_RuntimeMustNotStart)
    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "transport me", "unknown": True}).encode("utf-8"),
    )

    assert response.status == 400
    assert "unknown" in cast(str, cast(dict[str, object], response.json())["error"])


def test_transport_persists_streamed_run_for_session_listing_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("stream replay\n", encoding="utf-8")
    create_runtime_app = _load_transport_app_factory()

    app = create_runtime_app(workspace=tmp_path)

    async def _direct_stream(_self: object, runtime: object, request: object) -> Any:
        for chunk in cast(Any, runtime).run_stream(request):
            yield chunk

    monkeypatch.setattr(type(app), "_stream_runtime_chunks", _direct_stream)

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
    assert [payload["kind"] for payload in stream_payloads].count("output") == 1
    assert stream_payloads[-1]["kind"] == "output"
    assert list_response.status == 200
    listed_row = cast(dict[str, object], cast(list[object], list_response.json())[0])
    updated_at = listed_row.pop("updated_at")
    assert isinstance(updated_at, int) and updated_at >= 1
    assert listed_row == {
        "session": {"id": "streamed-session"},
        "status": "completed",
        "turn": 1,
        "prompt": "read sample.txt",
    }
    assert replay_response.status == 200
    assert cast(dict[str, object], replay_payload["session"])["session"] == {"id": "streamed-session"}
    assert cast(dict[str, object], replay_payload["session"])["status"] == "completed"
    assert cast(dict[str, object], replay_payload["session"])["turn"] == 1
    _assert_runtime_session_metadata(
        cast(dict[str, object], replay_payload["session"])["metadata"],
        workspace=tmp_path,
    )
    assert replay_payload["output"] == "Read 1 line(s) from sample.txt."
    _assert_ordered_event_types(
        _event_types_from_payload_events(replay_payload),
        [
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
        ],
    )


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
    assert response.json() == _error_body("prompt must be a non-empty string")


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
    assert response.json() == _error_body("parent session does not exist: missing-parent")


def test_transport_allows_parent_session_while_parent_stream_request_is_active(
    tmp_path: Path,
) -> None:
    _, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    service_module = importlib.import_module("voidcode.runtime.service")
    active_registry = service_module.ACTIVE_SESSION_REGISTRY

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(workspace=tmp_path),
    )

    parent_started = threading.Event()
    allow_parent_to_finish = threading.Event()

    original_register = active_registry.register

    def _register_and_signal(
        *,
        workspace: Path,
        session_id: str,
        run_id: str,
        metadata: dict[str, object],
    ) -> object:
        result = original_register(
            workspace=workspace,
            session_id=session_id,
            run_id=run_id,
            metadata=metadata,
        )
        if session_id == "leader-session":
            parent_started.set()
            allow_parent_to_finish.wait(timeout=5)
        return result

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
