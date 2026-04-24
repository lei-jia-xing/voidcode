from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, asdict
from types import ModuleType
from typing import Any, get_type_hints

import pytest


def _runtime_module() -> ModuleType:
    return importlib.import_module("voidcode.runtime")


def _graph_module() -> ModuleType:
    return importlib.import_module("voidcode.graph")


def _tools_module() -> ModuleType:
    return importlib.import_module("voidcode.tools")


def test_contract_modules_export_expected_symbols() -> None:
    runtime = _runtime_module()
    graph = _graph_module()
    tools = _tools_module()

    session_ref = runtime.SessionRef(id="session-1", parent_id="session-parent")
    session = runtime.SessionState(session=session_ref, status="running", turn=1)
    event = runtime.EventEnvelope(
        session_id="session-1",
        sequence=1,
        event_type="session.started",
        source="runtime",
    )
    tool = tools.ToolDefinition(
        name="read",
        description="Read a file",
        input_schema={"path": "string"},
    )
    call = tools.ToolCall(tool_name=tool.name, arguments={"path": "README.md"})
    runtime_request = runtime.RuntimeRequest(
        prompt="Inspect the repo",
        session_id=session.session.id,
        parent_session_id=session.session.parent_id,
    )
    background_task_request = runtime.BackgroundTaskRequestSnapshot(
        prompt=runtime_request.prompt,
        session_id=session.session.id,
        parent_session_id=runtime_request.parent_session_id,
    )
    background_task = runtime.BackgroundTaskState(
        task=runtime.BackgroundTaskRef(id="task-1"),
        request=background_task_request,
        created_at=1,
        updated_at=1,
    )
    background_task_result = runtime.BackgroundTaskResult(
        task_id=background_task.task.id,
        parent_session_id=background_task.parent_session_id,
        child_session_id=background_task.session_id,
        status=background_task.status,
    )
    session_summary = runtime.StoredSessionSummary(
        session=session.session,
        status="completed",
        turn=1,
        prompt=runtime_request.prompt,
        updated_at=1,
    )
    runtime_response = runtime.RuntimeResponse(session=session, events=(event,), output="done")
    stream_chunk = runtime.RuntimeStreamChunk(kind="event", session=session, event=event)
    graph_request = graph.GraphRunRequest(session=session, prompt=runtime_request.prompt)

    assert session.session.id == "session-1"
    assert session.session.parent_id == "session-parent"
    assert session.session.kind == "child"
    assert event.sequence == 1
    assert tool.read_only is True
    assert call.arguments == {"path": "README.md"}
    assert runtime_response.events == (event,)
    assert stream_chunk.event == event
    assert background_task.task.id == "task-1"
    assert background_task.request.prompt == runtime_request.prompt
    assert background_task.parent_session_id == "session-parent"
    assert background_task_result.task_id == "task-1"
    assert background_task_result.result_available is False
    assert background_task_result.delegated_event.delegation.delegated_task_id == "task-1"
    assert background_task_result.delegated_event.message.status == "queued"
    assert session_summary.session.id == "session-1"
    assert graph_request.available_tools == ()


def test_contracts_are_frozen_and_isolate_default_state() -> None:
    runtime = _runtime_module()

    first_session = runtime.SessionState(session=runtime.SessionRef(id="session-1"))
    second_session = runtime.SessionState(session=runtime.SessionRef(id="session-2"))
    first_task = runtime.BackgroundTaskState(
        task=runtime.BackgroundTaskRef(id="task-1"),
        request=runtime.BackgroundTaskRequestSnapshot(prompt="go"),
    )
    second_task = runtime.BackgroundTaskState(
        task=runtime.BackgroundTaskRef(id="task-2"),
        request=runtime.BackgroundTaskRequestSnapshot(prompt="go"),
    )

    first_session.metadata["workspace"] = "/tmp/project"
    first_task.request.metadata["workspace"] = "/tmp/project"

    assert second_session.metadata == {}
    assert second_task.request.metadata == {}

    with pytest.raises(FrozenInstanceError):
        first_session.turn = 3


def test_tool_result_validates_status_consistently() -> None:
    tools = _tools_module()

    with pytest.raises(ValueError, match="error results"):
        tools.ToolResult(tool_name="bash", status="error")

    with pytest.raises(ValueError, match="successful results"):
        tools.ToolResult(tool_name="bash", status="ok", error="unexpected")


def test_runtime_and_graph_protocols_are_runtime_checkable() -> None:
    runtime_module = _runtime_module()

    class StubRuntime:
        def run(self, request: Any) -> Any:
            typed_request = runtime_module.RuntimeRequest(**asdict(request))
            session = runtime_module.SessionState(
                session=runtime_module.SessionRef(id=typed_request.session_id or "new-session")
            )
            event = runtime_module.EventEnvelope(
                session_id=session.session.id,
                sequence=1,
                event_type="runtime.completed",
                source="runtime",
            )
            return runtime_module.RuntimeResponse(
                session=session,
                events=(event,),
                output=typed_request.prompt.upper(),
            )

        def run_stream(self, request: Any) -> Iterator[Any]:
            typed_request = runtime_module.RuntimeRequest(**asdict(request))
            session = runtime_module.SessionState(
                session=runtime_module.SessionRef(id=typed_request.session_id or "new-session"),
                status="running",
            )
            yield runtime_module.RuntimeStreamChunk(
                kind="event",
                session=session,
                event=runtime_module.EventEnvelope(
                    session_id=session.session.id,
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                ),
            )
            yield runtime_module.RuntimeStreamChunk(
                kind="output",
                session=runtime_module.SessionState(
                    session=session.session,
                    status="completed",
                ),
                output=typed_request.prompt.upper(),
            )

    runtime = StubRuntime()

    assert isinstance(runtime, runtime_module.RuntimeEntrypoint)
    assert isinstance(runtime, runtime_module.StreamingRuntimeEntrypoint)
    assert runtime.run(runtime_module.RuntimeRequest(prompt="hello")).output == "HELLO"
    stream = runtime.run_stream(runtime_module.RuntimeRequest(prompt="hello"))
    assert isinstance(stream, Iterator)
    assert [chunk.session.status for chunk in stream] == ["running", "completed"]


def test_contract_types_expose_explicit_annotations() -> None:
    runtime = _runtime_module()
    tools = _tools_module()

    runtime_request_hints = get_type_hints(runtime.RuntimeRequest)
    background_task_result_hints = get_type_hints(runtime.BackgroundTaskResult)
    background_task_hints = get_type_hints(runtime.BackgroundTaskState)
    tool_result_hints = get_type_hints(tools.ToolResult)

    assert runtime_request_hints["prompt"] is str
    assert background_task_result_hints["task_id"] is str
    assert str(background_task_result_hints["status"]) == "BackgroundTaskStatus"
    assert str(runtime_request_hints["parent_session_id"]) == "str | None"
    assert str(background_task_hints["status"]) == "BackgroundTaskStatus"
    assert tool_result_hints["tool_name"] is str
    assert str(tool_result_hints["status"]) == "ToolResultStatus"


def test_runtime_stream_chunk_validates_required_fields() -> None:
    runtime = _runtime_module()

    session = runtime.SessionState(session=runtime.SessionRef(id="session-1"))
    event = runtime.EventEnvelope(
        session_id="session-1",
        sequence=1,
        event_type="runtime.request_received",
        source="runtime",
    )

    event_chunk = runtime.RuntimeStreamChunk(kind="event", session=session, event=event)
    output_chunk = runtime.RuntimeStreamChunk(kind="output", session=session, output="done")

    assert event_chunk.event == event
    assert output_chunk.output == "done"

    with pytest.raises(ValueError, match="event chunks require an event"):
        _ = runtime.RuntimeStreamChunk(kind="event", session=session)

    with pytest.raises(ValueError, match="output chunks require output content"):
        _ = runtime.RuntimeStreamChunk(kind="output", session=session)


def test_runtime_contracts_allow_additive_future_event_types() -> None:
    runtime = _runtime_module()
    events_module = importlib.import_module("voidcode.runtime.events")

    session = runtime.SessionState(session=runtime.SessionRef(id="session-1"))
    event = runtime.EventEnvelope(
        session_id="session-1",
        sequence=1,
        event_type=events_module.RUNTIME_MEMORY_REFRESHED,
        source="runtime",
        payload={"summary_version": 2},
    )
    response = runtime.RuntimeResponse(session=session, events=(event,))

    assert get_type_hints(runtime.EventEnvelope)["event_type"] is str
    assert response.events[0].event_type == events_module.RUNTIME_MEMORY_REFRESHED
    assert asdict(response.events[0])["event_type"] == events_module.RUNTIME_MEMORY_REFRESHED


def test_event_envelope_exposes_typed_delegated_lifecycle_payload() -> None:
    runtime = _runtime_module()

    event = runtime.EventEnvelope(
        session_id="leader-session",
        sequence=2,
        event_type="runtime.background_task_completed",
        source="runtime",
        payload={
            "task_id": "task-1",
            "parent_session_id": "leader-session",
            "child_session_id": "child-session",
            "requested_child_session_id": "child-requested",
            "status": "completed",
            "summary_output": "Completed: delegated work",
            "result_available": True,
            "delegation": {
                "delegated_task_id": "task-1",
                "parent_session_id": "leader-session",
                "child_session_id": "child-session",
                "requested_child_session_id": "child-requested",
                "routing": {"mode": "background", "category": "quick"},
                "lifecycle_status": "completed",
                "result_available": True,
                "approval_blocked": False,
                "cancellation_cause": None,
                "approval_request_id": None,
                "question_request_id": None,
                "selected_preset": "worker",
                "selected_execution_engine": "provider",
            },
            "message": {
                "kind": "delegated_lifecycle",
                "status": "completed",
                "summary_output": "Completed: delegated work",
                "error": None,
                "approval_blocked": False,
                "result_available": True,
            },
        },
    )

    delegated = event.delegated_lifecycle

    assert delegated is not None
    assert delegated.delegation.delegated_task_id == "task-1"
    assert delegated.delegation.routing is not None
    assert delegated.delegation.routing.category == "quick"
    assert delegated.message.summary_output == "Completed: delegated work"
    assert delegated.message.status == "completed"
