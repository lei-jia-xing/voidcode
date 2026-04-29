"""Executable parity inventory for HTTP transport delegated-lifecycle day-1 surface.

These tests assert that the HTTP transport exposes (or is expected to expose)
every delegated-lifecycle capability that the runtime already supports.

Day-1 delegated lifecycle surfaces covered:
- create (POST /api/runtime/run/stream with parent_session_id)
- status (GET /api/sessions/{id} for child session replay)
- output (GET /api/sessions/{id}/result for child session result)
- cancel (POST /api/tasks/{id}/cancel)
- list (GET /api/sessions lists child sessions with parent_id)
- list tasks (GET /api/tasks and GET /api/sessions/{parent}/tasks)
- approval resolution (POST /api/sessions/{id}/approval)
- question resolution (POST /api/sessions/{id}/question)
- restart / resume (GET /api/sessions/{id} replays from SQLite)
- parent-visible events (runtime.background_task_* and runtime.acp_delegated_lifecycle)
- notification lifecycle (GET/POST /api/notifications)
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from voidcode.runtime.task import is_background_task_terminal

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


class SessionRefLike(Protocol):
    id: str
    parent_id: str | None


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
    parent_session_id: str | None
    metadata: dict[str, object]
    allocate_session_id: bool


class StoredSessionSummaryLike(Protocol):
    session: SessionRefLike
    status: str
    turn: int
    prompt: str
    updated_at: int


class BackgroundTaskRefLike(Protocol):
    id: str


class BackgroundTaskStateLike(Protocol):
    task: BackgroundTaskRefLike
    status: str
    approval_request_id: str | None
    child_session_id: str | None


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


class RuntimeRequestFactory(Protocol):
    def __call__(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RuntimeRequestLike: ...


class RuntimeRunner(Protocol):
    def run(self, request: RuntimeRequestLike) -> RuntimeResponseLike: ...

    def start_background_task(self, request: RuntimeRequestLike) -> BackgroundTaskStateLike: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskStateLike: ...


class RuntimeFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        permission_policy: object | None = None,
        graph: object | None = None,
    ) -> RuntimeRunner: ...


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
    def __call__(self, *, id: str, parent_id: str | None = None) -> SessionRefLike: ...


class SessionStateFactory(Protocol):
    def __call__(
        self,
        *,
        session: object,
        status: str,
        turn: int,
        metadata: dict[str, object] | None = None,
    ) -> SessionLike: ...


sys_path = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(sys_path))


def _load_transport_app_factory() -> TransportAppFactory:
    runtime_module = importlib.import_module("voidcode.runtime")
    return cast(TransportAppFactory, runtime_module.create_runtime_app)


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    return (
        cast(RuntimeRequestFactory, contracts_module.RuntimeRequest),
        cast(RuntimeFactory, service_module.VoidCodeRuntime),
    )


def _load_stream_types() -> tuple[
    RuntimeStreamChunkFactory, SessionRefFactory, SessionStateFactory
]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    session_module = importlib.import_module("voidcode.runtime.session")
    return (
        cast(RuntimeStreamChunkFactory, contracts_module.RuntimeStreamChunk),
        cast(SessionRefFactory, session_module.SessionRef),
        cast(SessionStateFactory, session_module.SessionState),
    )


def _run_app(
    app: TransportAppLike,
    *,
    method: str,
    path: str,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
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
    return (
        cast(int, start_message["status"]),
        headers,
        b"".join(body_parts),
    )


def _parse_sse_frames(body: bytes) -> list[dict[str, object]]:
    frames = [frame for frame in body.decode("utf-8").split("\n\n") if frame]
    payloads: list[dict[str, object]] = []
    for frame in frames:
        prefix = "data: "
        assert frame.startswith(prefix)
        payloads.append(cast(dict[str, object], json.loads(frame[len(prefix) :])))
    return payloads


def _wait_for_background_task_terminal(
    runtime: RuntimeRunner,
    task_id: str,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = runtime.load_background_task(task_id)
        if is_background_task_terminal(task.status):
            return
        time.sleep(0.01)
    raise AssertionError(f"background task did not reach terminal state: {task_id}")


def _wait_for_background_task_approval(
    runtime: RuntimeRunner,
    task_id: str,
    timeout: float = 3.0,
) -> BackgroundTaskStateLike:
    deadline = time.monotonic() + timeout
    last_task: BackgroundTaskStateLike | None = None
    while time.monotonic() < deadline:
        task = runtime.load_background_task(task_id)
        last_task = task
        if task.approval_request_id is not None:
            return task
        time.sleep(0.01)
    raise AssertionError(
        "background task did not reach approval wait: "
        f"{task_id}, last_status={last_task.status if last_task is not None else None!r}"
    )


def test_http_run_stream_accepts_parent_session_id_for_delegation() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state = _load_stream_types()

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.prompt == "child task"
            assert request.parent_session_id == "leader-session"
            yield runtime_stream_chunk(
                kind="output",
                session=session_state(
                    session=session_ref(id="child-session", parent_id="leader-session"),
                    status="completed",
                    turn=1,
                    metadata={},
                ),
                output="done",
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())
    status, _headers, body = _run_app(
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

    assert status == 200
    payloads = _parse_sse_frames(body)
    assert len(payloads) == 1
    session_ref_payload = cast(dict[str, object], payloads[0]["session"])["session"]
    assert cast(dict[str, object], session_ref_payload)["parent_id"] == "leader-session"


def test_http_sessions_list_shows_child_session_parent_id(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    _ = runtime.run(runtime_request(prompt="read sample.txt", parent_session_id="leader-session"))

    app = create_runtime_app(workspace=tmp_path)
    status, _headers, body = _run_app(app, method="GET", path="/api/sessions")

    assert status == 200
    sessions = cast(list[dict[str, object]], json.loads(body.decode("utf-8")))
    child_sessions = [
        s
        for s in sessions
        if cast(dict[str, object], s["session"]).get("parent_id") == "leader-session"
    ]
    assert len(child_sessions) >= 1


def test_http_session_replay_returns_child_session_status(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    child = runtime.run(
        runtime_request(prompt="read sample.txt", parent_session_id="leader-session")
    )
    child_id = child.session.session.id

    app = create_runtime_app(workspace=tmp_path)
    status, _headers, body = _run_app(app, method="GET", path=f"/api/sessions/{child_id}")

    assert status == 200
    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    assert cast(dict[str, object], payload["session"])["status"] == "completed"
    assert (
        cast(dict[str, object], cast(dict[str, object], payload["session"])["session"])["parent_id"]
        == "leader-session"
    )


def test_http_session_result_returns_child_output(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    child = runtime.run(
        runtime_request(prompt="read sample.txt", parent_session_id="leader-session")
    )
    child_id = child.session.session.id

    app = create_runtime_app(workspace=tmp_path)
    status, _headers, body = _run_app(app, method="GET", path=f"/api/sessions/{child_id}/result")

    assert status == 200
    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    assert payload["status"] == "completed"
    assert payload["output"] is not None
    assert "transcript" in payload


def test_http_approval_resolution_resumes_waiting_child_session(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    waiting = runtime.run(
        runtime_request(
            prompt="write child.txt delegated",
            session_id="child-approval-session",
            parent_session_id="leader-session",
        )
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    status, _headers, body = _run_app(
        app,
        method="POST",
        path="/api/sessions/child-approval-session/approval",
        body=json.dumps(
            {
                "request_id": approval_request_id,
                "decision": "allow",
            }
        ).encode("utf-8"),
    )

    assert status == 200
    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    assert cast(dict[str, object], payload["session"])["status"] == "completed"


def test_http_session_replay_persists_across_runtime_reinstantiation(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    (tmp_path / "sample.txt").write_text("persist me\n", encoding="utf-8")

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="persist-session"))

    app = importlib.import_module("voidcode.runtime").create_runtime_app(workspace=tmp_path)
    status, _headers, body = _run_app(app, method="GET", path="/api/sessions/persist-session")

    assert status == 200
    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    assert cast(dict[str, object], payload["session"])["status"] == "completed"
    assert payload["output"] == "persist me"


def test_http_notifications_surface_approval_blocked_for_child_session(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    _ = runtime.run(
        runtime_request(prompt="write child.txt delegated", session_id="child-notify-session")
    )

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    status, _headers, body = _run_app(app, method="GET", path="/api/notifications")

    assert status == 200
    notifications = cast(list[dict[str, object]], json.loads(body.decode("utf-8")))
    assert len(notifications) == 1
    assert notifications[0]["kind"] == "approval_blocked"
    assert notifications[0]["status"] == "unread"


def test_http_background_task_create_returns_task_identity_and_routing() -> None:
    create_runtime_app = _load_transport_app_factory()
    task_module = importlib.import_module("voidcode.runtime.task")

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def start_background_task(self, request: RuntimeRequestLike) -> object:
            assert request.prompt == "delegate this"
            assert request.parent_session_id == "leader-session"
            assert request.metadata == {
                "delegation": {
                    "mode": "background",
                    "subagent_type": "explore",
                    "description": "Inspect logs",
                }
            }
            return task_module.BackgroundTaskState(
                task=task_module.BackgroundTaskRef(id="task-123"),
                status="queued",
                request=task_module.BackgroundTaskRequestSnapshot(
                    prompt=request.prompt,
                    session_id="child-requested",
                    parent_session_id=request.parent_session_id,
                    metadata=dict(request.metadata),
                    allocate_session_id=False,
                ),
                created_at=1,
                updated_at=1,
            )

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())
    status, _headers, body = _run_app(
        app,
        method="POST",
        path="/api/tasks",
        body=json.dumps(
            {
                "prompt": "delegate this",
                "session_id": "child-requested",
                "parent_session_id": "leader-session",
                "metadata": {
                    "delegation": {
                        "mode": "background",
                        "subagent_type": "explore",
                        "description": "Inspect logs",
                    }
                },
            }
        ).encode("utf-8"),
    )

    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))

    assert status == 201
    assert payload["task"] == {"id": "task-123"}
    assert payload["parent_session_id"] == "leader-session"
    assert payload["requested_child_session_id"] == "child-requested"
    assert payload["routing"] == {
        "mode": "background",
        "subagent_type": "explore",
        "description": "Inspect logs",
    }


def test_http_background_task_status_endpoint_returns_runtime_state() -> None:
    create_runtime_app = _load_transport_app_factory()
    task_module = importlib.import_module("voidcode.runtime.task")

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def load_background_task(self, task_id: str) -> object:
            assert task_id == "task-123"
            return task_module.BackgroundTaskState(
                task=task_module.BackgroundTaskRef(id="task-123"),
                status="running",
                request=task_module.BackgroundTaskRequestSnapshot(
                    prompt="delegate this",
                    session_id="child-requested",
                    parent_session_id="leader-session",
                    metadata={"delegation": {"mode": "background", "category": "quick"}},
                ),
                session_id="child-session",
                approval_request_id="approval-1",
                created_at=1,
                updated_at=2,
                started_at=2,
            )

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())
    status, _headers, body = _run_app(app, method="GET", path="/api/tasks/task-123")

    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))

    assert status == 200
    assert payload["status"] == "running"
    assert payload["child_session_id"] == "child-session"
    assert payload["approval_request_id"] == "approval-1"
    assert payload["routing"] == {"mode": "background", "category": "quick"}


def test_http_background_task_cancel_endpoint_returns_cancelled_state() -> None:
    create_runtime_app = _load_transport_app_factory()
    task_module = importlib.import_module("voidcode.runtime.task")

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def cancel_background_task(self, task_id: str) -> object:
            assert task_id == "task-123"
            return task_module.BackgroundTaskState(
                task=task_module.BackgroundTaskRef(id="task-123"),
                status="cancelled",
                request=task_module.BackgroundTaskRequestSnapshot(
                    prompt="delegate this",
                    parent_session_id="leader-session",
                ),
                cancellation_cause="parent_cancelled",
                error="cancelled before start",
                created_at=1,
                updated_at=2,
                cancel_requested_at=2,
            )

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())

    status, _headers, body = _run_app(
        app,
        method="POST",
        path="/api/tasks/task-123/cancel",
        body=b"{}",
    )

    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))

    assert status == 200
    assert payload["status"] == "cancelled"
    assert payload["cancellation_cause"] == "parent_cancelled"
    assert payload["error"] == "cancelled before start"


def test_http_background_task_list_endpoints_expose_global_and_parent_scoped_views() -> None:
    create_runtime_app = _load_transport_app_factory()
    task_module = importlib.import_module("voidcode.runtime.task")

    all_tasks = (
        task_module.StoredBackgroundTaskSummary(
            task=task_module.BackgroundTaskRef(id="task-1"),
            status="queued",
            prompt="Investigate",
            session_id=None,
            error=None,
            created_at=1,
            updated_at=1,
        ),
        task_module.StoredBackgroundTaskSummary(
            task=task_module.BackgroundTaskRef(id="task-2"),
            status="completed",
            prompt="Summarize",
            session_id="child-session",
            error=None,
            created_at=2,
            updated_at=3,
        ),
    )

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def list_background_tasks(self) -> tuple[object, ...]:
            return all_tasks

        def list_background_tasks_by_parent_session(
            self, *, parent_session_id: str
        ) -> tuple[object, ...]:
            assert parent_session_id == "leader-session"
            return (all_tasks[1],)

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())

    status, _headers, body = _run_app(app, method="GET", path="/api/tasks")
    parent_status, _parent_headers, parent_body = _run_app(
        app,
        method="GET",
        path="/api/sessions/leader-session/tasks",
    )

    payload = cast(list[dict[str, object]], json.loads(body.decode("utf-8")))
    parent_payload = cast(list[dict[str, object]], json.loads(parent_body.decode("utf-8")))

    assert status == 200
    assert [item["task"] for item in payload] == [{"id": "task-1"}, {"id": "task-2"}]
    assert parent_status == 200
    assert parent_payload == [
        {
            "task": {"id": "task-2"},
            "status": "completed",
            "prompt": "Summarize",
            "session_id": "child-session",
            "error": None,
            "created_at": 2,
            "updated_at": 3,
        }
    ]


def test_http_background_task_output_endpoint_surfaces_runtime_truth_and_fallback() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_module = importlib.import_module("voidcode.runtime")
    task_module = importlib.import_module("voidcode.runtime.task")

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def load_background_task_result(self, task_id: str) -> object:
            assert task_id == "task-123"
            return runtime_module.BackgroundTaskResult(
                task_id="task-123",
                status="failed",
                parent_session_id="leader-session",
                requested_child_session_id="child-requested",
                child_session_id="child-missing",
                routing=task_module.SubagentRoutingIdentity(
                    mode="background",
                    subagent_type="explore",
                    description="Inspect logs",
                ),
                summary_output="Failed: delegated work",
                error="unknown session: child-missing",
                result_available=True,
            )

        def session_result(self, *, session_id: str) -> object:
            assert session_id == "child-missing"
            raise ValueError("unknown session: child-missing")

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())

    status, _headers, body = _run_app(app, method="GET", path="/api/tasks/task-123/output")

    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))

    assert status == 200
    assert cast(dict[str, object], payload["task"])["task_id"] == "task-123"
    assert cast(dict[str, object], payload["task"])["routing"] == {
        "mode": "background",
        "subagent_type": "explore",
        "description": "Inspect logs",
    }
    delegation = cast(dict[str, object], cast(dict[str, object], payload["task"])["delegation"])
    assert delegation["delegated_task_id"] == "task-123"
    message = cast(dict[str, object], cast(dict[str, object], payload["task"])["message"])
    assert message["status"] == "failed"
    assert payload["session_result"] is None
    assert payload["output"] == "Failed: delegated work"


def test_http_background_task_output_endpoint_exposes_typed_delegated_payload_for_live_runtime(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    started = runtime.start_background_task(
        runtime_request(prompt="read sample.txt", parent_session_id="leader-session")
    )

    import time

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        task = runtime.load_background_task(started.task.id)
        if is_background_task_terminal(task.status):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("background task did not reach terminal state")

    app = create_runtime_app(workspace=tmp_path)
    status, _headers, body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}/output",
    )

    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    task_payload = cast(dict[str, object], payload["task"])
    delegation = cast(dict[str, object], task_payload["delegation"])
    message = cast(dict[str, object], task_payload["message"])

    assert status == 200
    assert task_payload["task_id"] == started.task.id
    assert task_payload["parent_session_id"] == "leader-session"
    assert delegation == {
        "parent_session_id": "leader-session",
        "requested_child_session_id": cast(str, task_payload["child_session_id"]),
        "child_session_id": cast(str, task_payload["child_session_id"]),
        "delegated_task_id": started.task.id,
        "approval_request_id": None,
        "question_request_id": None,
        "routing": None,
        "selected_preset": None,
        "selected_execution_engine": None,
        "lifecycle_status": "completed",
        "approval_blocked": False,
        "result_available": True,
        "cancellation_cause": None,
    }
    assert str(message["summary_output"]).startswith("Completed child session ")
    assert "Completed: hello" not in str(message["summary_output"])
    assert message == {
        "kind": "delegated_lifecycle",
        "status": "completed",
        "summary_output": message["summary_output"],
        "error": None,
        "approval_blocked": False,
        "result_available": True,
    }
    assert payload["output"] == "hello"


def test_http_background_subagent_restart_reconcile_retrieves_terminal_task_result(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    (tmp_path / "sample.txt").write_text("restart delegated result\n", encoding="utf-8")

    first_runtime = runtime_class(workspace=tmp_path)
    _ = first_runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    started = first_runtime.start_background_task(
        runtime_request(prompt="read sample.txt", parent_session_id="leader-session")
    )
    _wait_for_background_task_terminal(first_runtime, started.task.id)

    app = create_runtime_app(workspace=tmp_path)
    status, _headers, body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}/output",
    )
    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    task_payload = cast(dict[str, object], payload["task"])
    delegation = cast(dict[str, object], task_payload["delegation"])

    assert status == 200
    assert task_payload["status"] == "completed"
    assert task_payload["parent_session_id"] == "leader-session"
    assert delegation["lifecycle_status"] == "completed"
    assert delegation["result_available"] is True
    assert payload["output"] == "restart delegated result"


def test_http_background_task_output_endpoint_preserves_empty_child_output(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_request, runtime_class = _load_runtime_types()

    class _EmptyOutputGraph:
        def step(
            self,
            request: object,
            tool_results: tuple[object, ...],
            *,
            session: object,
        ) -> object:
            _ = tool_results, session
            return type("_Step", (), {"output": "", "is_finished": True, "tool_call": None})()

    runtime = runtime_class(workspace=tmp_path, graph=_EmptyOutputGraph())
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    started = runtime.start_background_task(
        runtime_request(prompt="empty child", parent_session_id="leader-session")
    )
    _wait_for_background_task_terminal(runtime, started.task.id)

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: runtime)

    status, _headers, body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}/output",
    )

    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))

    assert status == 200
    assert payload["session_result"] is not None
    assert payload["output"] == ""


def test_http_tasks_endpoints_cover_real_runtime_completed_lifecycle(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    runtime = runtime_class(workspace=tmp_path)
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    started = runtime.start_background_task(
        runtime_request(prompt="read sample.txt", parent_session_id="leader-session")
    )
    _wait_for_background_task_terminal(runtime, started.task.id)

    app = create_runtime_app(workspace=tmp_path)
    status, _headers, status_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}",
    )
    output_status, _output_headers, output_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}/output",
    )
    list_status, _list_headers, list_body = _run_app(app, method="GET", path="/api/tasks")
    parent_status, _parent_headers, parent_body = _run_app(
        app,
        method="GET",
        path="/api/sessions/leader-session/tasks",
    )

    status_payload = cast(dict[str, object], json.loads(status_body.decode("utf-8")))
    output_payload = cast(dict[str, object], json.loads(output_body.decode("utf-8")))
    list_payload = cast(list[dict[str, object]], json.loads(list_body.decode("utf-8")))
    parent_payload = cast(list[dict[str, object]], json.loads(parent_body.decode("utf-8")))
    task_payload = cast(dict[str, object], output_payload["task"])

    assert status == 200
    assert output_status == 200
    assert list_status == 200
    assert parent_status == 200
    assert status_payload["status"] == "completed"
    assert status_payload["parent_session_id"] == "leader-session"
    assert status_payload["result_available"] is True
    assert task_payload["task_id"] == started.task.id
    assert task_payload["status"] == "completed"
    assert task_payload["approval_blocked"] is False
    assert output_payload["output"] == "hello"
    assert any(item["task"] == {"id": started.task.id} for item in list_payload)
    assert any(item["task"] == {"id": started.task.id} for item in parent_payload)


def test_http_tasks_endpoints_cover_real_runtime_waiting_approval_and_cancel(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    (tmp_path / "sample.txt").write_text("leader\n", encoding="utf-8")
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    started = runtime.start_background_task(
        runtime_request(
            prompt="write child.txt delegated",
            parent_session_id="leader-session",
        )
    )
    waiting = _wait_for_background_task_approval(runtime, started.task.id)

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    status, _headers, status_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}",
    )
    output_status, _output_headers, output_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}/output",
    )
    cancel_status, _cancel_headers, cancel_body = _run_app(
        app,
        method="POST",
        path=f"/api/tasks/{started.task.id}/cancel",
        body=b"{}",
    )

    status_payload = cast(dict[str, object], json.loads(status_body.decode("utf-8")))
    output_payload = cast(dict[str, object], json.loads(output_body.decode("utf-8")))
    cancel_payload = cast(dict[str, object], json.loads(cancel_body.decode("utf-8")))
    task_payload = cast(dict[str, object], output_payload["task"])
    delegation = cast(dict[str, object], task_payload["delegation"])
    message = cast(dict[str, object], task_payload["message"])

    assert status == 200
    assert output_status == 200
    assert cancel_status == 200
    assert status_payload["status"] == "running"
    assert status_payload["approval_request_id"] == waiting.approval_request_id
    assert task_payload["task_id"] == started.task.id
    assert task_payload["approval_request_id"] == waiting.approval_request_id
    assert task_payload["approval_blocked"] is True
    assert delegation["lifecycle_status"] == "waiting_approval"
    assert delegation["approval_blocked"] is True
    assert message["status"] == "waiting_approval"
    assert message["approval_blocked"] is True
    assert cast(dict[str, object], output_payload["session_result"])["status"] == "waiting"
    assert cast(dict[str, object], output_payload["session_result"])["output"] is None
    assert "Approval blocked on write_file: write_file child.txt" in cast(
        str, output_payload["output"]
    )
    assert cancel_payload["status"] == "cancelled"
    assert (
        cancel_payload["cancellation_cause"]
        == "cancelled by parent while child session was waiting"
    )
    assert cancel_payload["error"] == "cancelled by parent while child session was waiting"


def test_http_approval_resolution_endpoint_resumes_real_waiting_background_task(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    (tmp_path / "sample.txt").write_text("leader\n", encoding="utf-8")
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    started = runtime.start_background_task(
        runtime_request(
            prompt="write child.txt delegated",
            parent_session_id="leader-session",
        )
    )
    waiting = _wait_for_background_task_approval(runtime, started.task.id)
    child_session_id = cast(str, waiting.child_session_id)
    approval_request_id = cast(str, waiting.approval_request_id)

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    resume_status, _resume_headers, resume_body = _run_app(
        app,
        method="POST",
        path=f"/api/sessions/{child_session_id}/approval",
        body=json.dumps({"request_id": approval_request_id, "decision": "allow"}).encode("utf-8"),
    )
    task_status, _task_headers, task_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}",
    )
    output_status, _output_headers, output_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}/output",
    )

    resume_payload = cast(dict[str, object], json.loads(resume_body.decode("utf-8")))
    task_payload = cast(dict[str, object], json.loads(task_body.decode("utf-8")))
    output_payload = cast(dict[str, object], json.loads(output_body.decode("utf-8")))

    assert resume_status == 200
    assert task_status == 200
    assert output_status == 200
    assert cast(dict[str, object], resume_payload["session"])["status"] == "completed"
    assert task_payload["status"] == "completed"
    assert cast(dict[str, object], output_payload["task"])["status"] == "completed"
    assert output_payload["output"] == "Wrote file successfully: child.txt"


def test_http_approval_resolution_preserves_stale_terminal_task_status_across_runtime_instances(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    create_runtime_app = _load_transport_app_factory()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(object, permission_module.PermissionPolicy(mode="ask"))

    runtime = runtime_class(workspace=tmp_path, permission_policy=permission_policy)
    (tmp_path / "sample.txt").write_text("leader\n", encoding="utf-8")
    _ = runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    started = runtime.start_background_task(
        runtime_request(
            prompt="write child.txt delegated",
            parent_session_id="leader-session",
        )
    )
    waiting = _wait_for_background_task_approval(runtime, started.task.id)
    child_session_id = cast(str, waiting.child_session_id)
    approval_request_id = cast(str, waiting.approval_request_id)

    task_store = cast(Any, runtime)._session_store
    _ = task_store.mark_background_task_terminal(
        workspace=tmp_path,
        task_id=started.task.id,
        status="failed",
        error="background task interrupted before completion",
    )

    app = create_runtime_app(
        workspace=tmp_path,
        runtime_factory=lambda: runtime_class(
            workspace=tmp_path,
            permission_policy=permission_policy,
        ),
    )
    resume_status, _resume_headers, resume_body = _run_app(
        app,
        method="POST",
        path=f"/api/sessions/{child_session_id}/approval",
        body=json.dumps({"request_id": approval_request_id, "decision": "allow"}).encode("utf-8"),
    )
    task_status, _task_headers, task_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}",
    )
    output_status, _output_headers, output_body = _run_app(
        app,
        method="GET",
        path=f"/api/tasks/{started.task.id}/output",
    )

    resume_payload = cast(dict[str, object], json.loads(resume_body.decode("utf-8")))
    task_payload = cast(dict[str, object], json.loads(task_body.decode("utf-8")))
    output_payload = cast(dict[str, object], json.loads(output_body.decode("utf-8")))
    output_task_payload = cast(dict[str, object], output_payload["task"])

    assert resume_status == 200
    assert task_status == 200
    assert output_status == 200
    assert cast(dict[str, object], resume_payload["session"])["status"] == "completed"
    assert task_payload["status"] == "failed"
    assert output_task_payload["status"] == "failed"
    assert output_payload["output"] == "Wrote file successfully: child.txt"


def test_http_run_stream_accepts_metadata_for_skills_and_max_steps() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state = _load_stream_types()

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.metadata.get("skills") == ["demo"]
            assert request.metadata.get("max_steps") == 10
            yield runtime_stream_chunk(
                kind="output",
                session=session_state(
                    session=session_ref(id="meta-session"),
                    status="completed",
                    turn=1,
                    metadata={},
                ),
                output="done",
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())
    status, _headers, _body = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps(
            {
                "prompt": "run with meta",
                "metadata": {"skills": ["demo"], "max_steps": 10},
            }
        ).encode("utf-8"),
    )

    assert status == 200


def test_http_run_stream_allocates_anonymous_session_when_no_session_id() -> None:
    create_runtime_app = _load_transport_app_factory()
    runtime_stream_chunk, session_ref, session_state = _load_stream_types()

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            assert request.session_id is None
            assert request.allocate_session_id is True
            yield runtime_stream_chunk(
                kind="output",
                session=session_state(
                    session=session_ref(id="anon-session"),
                    status="completed",
                    turn=1,
                    metadata={},
                ),
                output="done",
            )

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

        def resume(self, session_id: str) -> RuntimeResponseLike:
            raise AssertionError(f"resume should not be called: {session_id}")

    app = create_runtime_app(workspace=Path("/tmp"), runtime_factory=lambda: StubRuntime())
    status, _headers, _body = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "anonymous run"}).encode("utf-8"),
    )

    assert status == 200


def test_http_question_resolution_resumes_waiting_session(tmp_path: Path) -> None:
    runtime_module = importlib.import_module("voidcode.runtime")
    create_runtime_app = _load_transport_app_factory()

    class StubRuntime:
        def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]:
            raise AssertionError(f"run_stream should not be called: {request}")

        def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]:
            return ()

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
            assert question_request_id == "q-1"
            assert len(responses) == 1
            return runtime_module.RuntimeResponse(
                session=runtime_module.SessionState(
                    session=runtime_module.SessionRef(id="question-session"),
                    status="completed",
                    turn=1,
                    metadata={},
                ),
                events=(
                    runtime_module.EventEnvelope(
                        session_id="question-session",
                        sequence=1,
                        event_type="runtime.question_answered",
                        source="runtime",
                        payload={"request_id": "q-1"},
                    ),
                ),
                output="answered",
            )

    app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: StubRuntime())
    status, _headers, body = _run_app(
        app,
        method="POST",
        path="/api/sessions/question-session/question",
        body=json.dumps(
            {
                "request_id": "q-1",
                "responses": [
                    {"header": "Choice", "answers": ["Option A"]},
                ],
            }
        ).encode("utf-8"),
    )

    assert status == 200
    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    assert cast(dict[str, object], payload["session"])["status"] == "completed"


def test_http_settings_update_and_read(tmp_path: Path) -> None:
    create_runtime_app = _load_transport_app_factory()
    app = create_runtime_app(workspace=tmp_path)

    status, _headers, body = _run_app(
        app,
        method="POST",
        path="/api/settings",
        body=json.dumps(
            {
                "provider": "opencode-go",
                "model": "opencode-go/glm-5",
            }
        ).encode("utf-8"),
    )
    assert status == 200

    status, _headers, body = _run_app(app, method="GET", path="/api/settings")
    assert status == 200
    payload = cast(dict[str, object], json.loads(body.decode("utf-8")))
    assert payload["provider"] == "opencode-go"
    assert payload["model"] == "opencode-go/glm-5"
