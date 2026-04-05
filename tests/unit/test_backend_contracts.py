from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import ModuleType
from typing import Any, get_type_hints

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

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

    session_ref = runtime.SessionRef(id="session-1")
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
    result = tools.ToolResult(tool_name=tool.name, status="ok", content="hello")
    runtime_request = runtime.RuntimeRequest(
        prompt="Inspect the repo", session_id=session.session.id
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
    graph_result = graph.GraphRunResult(session=session, events=(event,), tool_results=(result,))

    assert session.session.id == "session-1"
    assert event.sequence == 1
    assert tool.read_only is True
    assert call.arguments == {"path": "README.md"}
    assert runtime_response.events == (event,)
    assert stream_chunk.event == event
    assert session_summary.session.id == "session-1"
    assert graph_request.available_tools == ()
    assert graph_result.tool_results == (result,)


def test_contracts_are_frozen_and_isolate_default_state() -> None:
    runtime = _runtime_module()

    first_session = runtime.SessionState(session=runtime.SessionRef(id="session-1"))
    second_session = runtime.SessionState(session=runtime.SessionRef(id="session-2"))

    first_session.metadata["workspace"] = "/tmp/project"

    assert second_session.metadata == {}

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
    graph_module = _graph_module()

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

    class StubGraphRunner:
        def run(self, request: Any) -> Any:
            typed_request = graph_module.GraphRunRequest(**asdict(request))
            return graph_module.GraphRunResult(
                session=typed_request.session,
                output=typed_request.prompt,
            )

    runtime = StubRuntime()
    graph_runner = StubGraphRunner()

    assert isinstance(runtime, runtime_module.RuntimeEntrypoint)
    assert isinstance(runtime, runtime_module.StreamingRuntimeEntrypoint)
    assert isinstance(graph_runner, graph_module.GraphRunner)
    assert runtime.run(runtime_module.RuntimeRequest(prompt="hello")).output == "HELLO"
    stream = runtime.run_stream(runtime_module.RuntimeRequest(prompt="hello"))
    assert isinstance(stream, Iterator)
    assert [chunk.session.status for chunk in stream] == ["running", "completed"]
    assert (
        graph_runner.run(
            graph_module.GraphRunRequest(
                session=runtime_module.SessionState(
                    session=runtime_module.SessionRef(id="session-1")
                ),
                prompt="hi",
            )
        ).output
        == "hi"
    )


def test_contract_types_expose_explicit_annotations() -> None:
    runtime = _runtime_module()
    graph = _graph_module()
    tools = _tools_module()

    runtime_request_hints = get_type_hints(runtime.RuntimeRequest)
    tool_result_hints = get_type_hints(tools.ToolResult)
    graph_runner_hints = get_type_hints(graph.GraphRunner.run)

    assert runtime_request_hints["prompt"] is str
    assert tool_result_hints["tool_name"] is str
    assert str(tool_result_hints["status"]) == "ToolResultStatus"
    assert graph_runner_hints["request"] is graph.GraphRunRequest
    assert graph_runner_hints["return"] is graph.GraphRunResult


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
