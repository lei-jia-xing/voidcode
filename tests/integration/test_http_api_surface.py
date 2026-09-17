"""Integration tests for the shipped HTTP API surface.

The transport's route table and its error codes are a client contract, so they
are pinned here rather than only described in docs: the OpenAPI document at
``/api/openapi.json`` must enumerate exactly the routes the transport serves, the
retired alias and query spelling must stay retired, and every error a consumer
routes on must carry its stable ``code``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.usefixtures("_deterministic_engine")


@pytest.fixture
def _deterministic_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    config_module = importlib.import_module("voidcode.runtime.config")
    monkeypatch.setattr(
        config_module,
        "_default_runtime_mcp_config",
        lambda: config_module.RuntimeMcpConfig(enabled=False),
    )
    monkeypatch.setattr(config_module, "_default_runtime_mcp_servers", lambda: {})


# Every (method, path) the transport serves, in OpenAPI spelling (path
# parameters lose their convertor suffix). ``/api/openapi.json`` itself is a
# route but is not part of the documented surface.
SHIPPED_ROUTE_TABLE: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/runtime/run/stream"),
        ("GET", "/api/sessions"),
        ("GET", "/api/tasks"),
        ("POST", "/api/tasks"),
        ("GET", "/api/notifications"),
        ("GET", "/api/settings"),
        ("POST", "/api/settings"),
        ("GET", "/api/workspaces"),
        ("POST", "/api/workspaces/open"),
        ("GET", "/api/providers"),
        ("GET", "/api/agents"),
        ("GET", "/api/skills"),
        ("GET", "/api/commands"),
        ("GET", "/api/status"),
        ("POST", "/api/status/mcp/retry"),
        ("GET", "/api/review"),
        ("GET", "/api/review/diff/{path}"),
        ("POST", "/api/notifications/{notification_id}/ack"),
        ("GET", "/api/tasks/{task_id}"),
        ("GET", "/api/tasks/{task_id}/output"),
        ("POST", "/api/tasks/{task_id}/cancel"),
        ("POST", "/api/tasks/{task_id}/retry"),
        ("POST", "/api/tasks/{task_id}/steer"),
        ("GET", "/api/sessions/{session_id}"),
        ("GET", "/api/sessions/{session_id}/events"),
        ("GET", "/api/sessions/{session_id}/tasks"),
        ("GET", "/api/sessions/{session_id}/delegated-context"),
        ("POST", "/api/sessions/{session_id}/approval"),
        ("POST", "/api/sessions/{session_id}/question"),
        ("GET", "/api/sessions/{session_id}/result"),
        ("GET", "/api/sessions/{session_id}/debug"),
        ("POST", "/api/sessions/{session_id}/undo"),
        ("POST", "/api/sessions/{session_id}/revert"),
        ("POST", "/api/sessions/{session_id}/unrevert"),
        ("POST", "/api/sessions/{session_id}/cancel"),
        ("POST", "/api/sessions/{session_id}/resume"),
        ("POST", "/api/sessions/{session_id}/steer"),
        ("GET", "/api/providers/{provider_name}/models"),
        ("GET", "/api/providers/{provider_name}/inspect"),
        ("POST", "/api/providers/{provider_name}/validate"),
    }
)


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _error_body(message: str, *, code: str | None = None) -> dict[str, object]:
    """The transport's error envelope: a message plus the runtime's optional code."""
    return {"error": message, "code": code}


def _drive(
    app: object,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    query_string: bytes = b"",
) -> tuple[_Response | None, BaseException | None]:
    """Drive one request, returning the response *and* any escaping error.

    Starlette's server-error middleware answers the request *and* re-raises, so
    the unhandled-failure contract needs both halves: what the client received
    and the exception the server still logs.
    """
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
        "headers": [],
    }
    error: BaseException | None = None
    try:
        asyncio.run(cast(Any, app)(scope, _receive, _send))
    except BaseException as exc:
        error = exc

    start_message = next((message for message in sent if message["type"] == "http.response.start"), None)
    if start_message is None:
        return None, error
    headers = {key.decode("utf-8").lower(): value.decode("utf-8") for key, value in cast(list[tuple[bytes, bytes]], start_message["headers"])}
    body_bytes = b"".join(cast(bytes, message.get("body", b"")) for message in sent if message["type"] == "http.response.body")
    return _Response(status=cast(int, start_message["status"]), headers=headers, body=body_bytes), error


def _run_app(
    app: object,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    query_string: bytes = b"",
) -> _Response:
    response, error = _drive(app, method=method, path=path, body=body, query_string=query_string)
    assert error is None, error
    assert response is not None
    return response


def _app(*, frontend_dist: Path | None = None) -> object:
    runtime_module = importlib.import_module("voidcode.runtime")
    return cast(Callable[..., object], runtime_module.create_runtime_app)(
        workspace=Path("/tmp/surface-workspace"),
        frontend_dist=frontend_dist,
    )


def _openapi_document(app: object) -> dict[str, Any]:
    response = _run_app(app, method="GET", path="/api/openapi.json")
    assert response.status == 200, response.body
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    return cast(dict[str, Any], response.json())


def test_openapi_document_lists_exactly_the_shipped_route_table() -> None:
    document = _openapi_document(_app())

    surface = {(method.upper(), path) for path, operations in document["paths"].items() for method in operations}
    assert surface == SHIPPED_ROUTE_TABLE
    assert not any("openapi" in path for path in document["paths"])


def test_openapi_document_carries_a_summary_and_tags_for_every_operation() -> None:
    document = _openapi_document(_app())

    untagged: list[tuple[str, str]] = []
    untitled: list[tuple[str, str]] = []
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if not operation.get("summary"):
                untitled.append((method, path))
            if not operation.get("tags"):
                untagged.append((method, path))
    assert untitled == []
    assert untagged == []
    assert sorted({tag for operations in document["paths"].values() for operation in operations.values() for tag in operation["tags"]}) == [
        "notifications",
        "providers",
        "review",
        "runtime",
        "sessions",
        "settings",
        "tasks",
        "workspaces",
    ]


def test_openapi_document_describes_the_stream_query_parameters() -> None:
    document = _openapi_document(_app())

    parameters = {
        parameter["name"]: parameter
        for parameter in document["paths"]["/api/sessions/{session_id}/events"]["get"]["parameters"]
        if parameter["in"] == "query"
    }
    assert set(parameters) == {"after_sequence", "follow", "show_thinking"}
    assert parameters["after_sequence"]["schema"]["default"] == 0
    assert parameters["follow"]["schema"]["default"] is False
    assert parameters["show_thinking"]["schema"]["default"] is False
    assert parameters["show_thinking"]["description"]
    assert parameters["show_thinking"]["schema"]["type"] == "boolean"

    run_stream = document["paths"]["/api/runtime/run/stream"]["post"]
    assert "show_thinking" in {parameter["name"] for parameter in run_stream["parameters"] if parameter["in"] == "query"}


def test_openapi_document_is_not_shadowed_by_the_spa_fallback(tmp_path: Path) -> None:
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    _ = (frontend_dist / "index.html").write_text("<!DOCTYPE html><title>shell</title>", encoding="utf-8")

    app = _app(frontend_dist=frontend_dist)

    document = _run_app(app, method="GET", path="/api/openapi.json")
    assert document.status == 200
    assert document.headers["content-type"] == "application/json; charset=utf-8"
    assert cast(dict[str, Any], document.json())["paths"]

    # The SPA fallback still answers client-side routes, and unknown API paths
    # stay JSON 404s rather than rendering the shell.
    assert _run_app(app, method="GET", path="/client/route").status == 200
    unknown = _run_app(app, method="GET", path="/api/not-a-route")
    assert unknown.status == 404
    assert unknown.json() == _error_body("not found")


def test_retired_interrupt_alias_is_not_routed() -> None:
    app = _app()

    response = _run_app(app, method="POST", path="/api/sessions/interrupt-session/interrupt")

    assert response.status == 404
    assert response.json() == _error_body("not found")
    assert not any("interrupt" in path for path in _openapi_document(app)["paths"])


def test_only_show_thinking_is_honoured_not_the_camel_case_twin() -> None:
    """The documented spelling is ``show_thinking``; ``showThinking`` is gone."""
    runtime_http = importlib.import_module("voidcode.runtime.http")
    runtime_contracts = importlib.import_module("voidcode.runtime.contracts")
    runtime_events = importlib.import_module("voidcode.runtime.events")
    runtime_session = importlib.import_module("voidcode.runtime.session")

    result = runtime_contracts.RuntimeSessionResult(
        session=runtime_session.SessionState(
            session=runtime_session.SessionRef(id="reasoning-session"),
            status="completed",
            metadata={},
        ),
        prompt="think",
        status="completed",
        summary="Completed",
        output="answer",
        transcript=(
            runtime_events.EventEnvelope(
                session_id="reasoning-session",
                sequence=1,
                event_type="runtime.reasoning_part",
                source="runtime",
                payload=runtime_events.runtime_reasoning_part_payload(text="private chain"),
            ),
        ),
        last_event_sequence=1,
    )

    class ReasoningResultRuntime:
        def session_result(self, *, session_id: str) -> object:
            return result

        def __exit__(self, *_: object) -> None:
            return None

    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, ReasoningResultRuntime))
    path = "/api/sessions/reasoning-session/result"

    shown = _run_app(app, method="GET", path=path, query_string=b"show_thinking=true").body
    camel = _run_app(app, method="GET", path=path, query_string=b"showThinking=true").body
    absent = _run_app(app, method="GET", path=path).body
    unused = _run_app(app, method="GET", path=path, query_string=b"show_thinking=banana").body

    assert b"private chain" in shown
    assert b"private chain" not in camel
    assert camel == absent
    # An unrecognised value is false, never a validation failure.
    assert unused == absent


def test_delegated_context_miss_carries_a_stable_code(tmp_path: Path) -> None:
    _write_sample(tmp_path)
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="plain-session"))

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(app, method="GET", path="/api/sessions/plain-session/delegated-context")

    assert response.status == 404
    assert response.json() == _error_body(
        "no delegated child context for session: plain-session",
        code="delegated_context_missing",
    )


def test_sealed_session_steer_carries_a_stable_code(tmp_path: Path) -> None:
    _write_sample(tmp_path)
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="sealed-session"))

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/sealed-session/steer",
        body=json.dumps({"content": "late message"}).encode("utf-8"),
    )

    assert response.status == 409
    payload = cast(dict[str, object], response.json())
    assert payload["code"] == "session_sealed"
    assert "sealed-session" in cast(str, payload["error"])


def test_missing_pending_approval_carries_a_stable_code(tmp_path: Path) -> None:
    _write_sample(tmp_path)
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="completed-session"))

    app = create_runtime_app(workspace=tmp_path)
    response = _run_app(
        app,
        method="POST",
        path="/api/sessions/completed-session/approval",
        body=json.dumps({"request_id": "missing-request", "decision": "allow"}).encode("utf-8"),
    )

    assert response.status == 409
    assert response.json() == _error_body(
        "no pending approval for session: completed-session",
        code="no_pending_approval",
    )


def test_plain_validation_failures_keep_a_null_code(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    invalid_body = _run_app(
        app,
        method="POST",
        path="/api/settings",
        body=b'{"provider": 5}',
    )
    assert invalid_body.status == 400
    assert invalid_body.json() == _error_body("provider must be a string when provided")

    unknown_route = _run_app(app, method="GET", path="/api/not-a-route")
    assert unknown_route.status == 404
    assert unknown_route.json() == _error_body("not found")

    wrong_method = _run_app(app, method="DELETE", path="/api/sessions")
    assert wrong_method.status == 405
    assert wrong_method.json() == _error_body("method not allowed")


def test_unhandled_api_failure_uses_the_error_envelope() -> None:
    """An unhandled failure on an API path must not leave the error contract."""

    class _CrashRuntime:
        def list_sessions(self) -> object:
            raise RuntimeError("session store exploded")

        def __exit__(self, *_: object) -> None:
            return None

    runtime_http = importlib.import_module("voidcode.runtime.http")
    app = runtime_http.RuntimeTransportApp(runtime_factory=cast(Any, _CrashRuntime))

    response, error = _drive(app, method="GET", path="/api/sessions")

    # The server still sees (and logs) the exception ...
    assert isinstance(error, RuntimeError)
    # ... while the client receives the transport's own envelope.
    assert response is not None
    assert response.status == 500
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == _error_body("internal server error")


def test_unhandled_static_failure_keeps_the_plain_text_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-API paths keep Starlette's own 500; the envelope is API-only."""
    app = _app()

    def _boom(path: str, method: str) -> object:
        raise RuntimeError("static exploded")

    monkeypatch.setattr(app, "_static_file_response", _boom)

    response, error = _drive(app, method="GET", path="/client/route")

    assert isinstance(error, RuntimeError)
    assert response is not None
    assert response.status == 500
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.body == b"Internal Server Error"


def _write_sample(tmp_path: Path) -> None:
    _ = (tmp_path / "sample.txt").write_text("surface payload\n", encoding="utf-8")


def _load_transport_app_factory() -> Callable[..., object]:
    runtime_module = importlib.import_module("voidcode.runtime")
    return cast(Callable[..., object], runtime_module.create_runtime_app)


def _load_runtime_types() -> tuple[Callable[..., Any], Callable[..., Any]]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    return (
        cast(Callable[..., Any], contracts_module.RuntimeRequest),
        cast(Callable[..., Any], service_module.VoidCodeRuntime),
    )
