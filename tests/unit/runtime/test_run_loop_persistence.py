from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voidcode.graph.contracts import GraphEvent, GraphRunRequest
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.policy import materialize_runtime_policy_snapshot
from voidcode.runtime.resume import RuntimeResumeCoordinator
from voidcode.runtime.service import RuntimeStreamChunk, SessionState, ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class _GraphStep:
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False


def _create_session_row(store: SqliteSessionStore, *, workspace: Path, session_id: str) -> None:
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=session_id,
        prompt="persistence probe",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )


def _runtime_with_store(tmp_path: Path, store: SqliteSessionStore) -> VoidCodeRuntime:
    return VoidCodeRuntime(workspace=tmp_path, session_store=store)


def _loaded_events(store: SqliteSessionStore, *, workspace: Path, session_id: str) -> tuple[EventEnvelope, ...]:
    return store.load_session(workspace=workspace, session_id=session_id).events


def _graph_request(runtime: VoidCodeRuntime, *, session_id: str, provider_stream: bool = False) -> tuple[SessionState, GraphRunRequest, ToolRegistry]:
    effective_config = runtime.effective_runtime_config()
    runtime_config_metadata = runtime._runtime_config_metadata()
    runtime_policy = materialize_runtime_policy_snapshot(
        persisted_session_policy=None,
        agent_preset=effective_config.agent.preset if effective_config.agent is not None else "leader",
        agent_manifest_id=effective_config.agent.preset if effective_config.agent is not None else "leader",
        runtime_config=runtime_config_metadata,
        request_metadata={},
        parent_snapshot=None,
    ).as_payload()
    session = SessionState(
        session=SessionRef(id=session_id),
        status="running",
        turn=0,
        metadata={
            "runtime_config": runtime_config_metadata,
            "runtime_policy": runtime_policy,
        },
    )
    prompt = "probe"
    tool_registry = runtime._tool_registry_for_effective_config(runtime.effective_runtime_config())
    context_window = runtime._prepare_provider_context_window(
        prompt=prompt,
        tool_results=(),
        session_metadata=session.metadata,
    )
    request = GraphRunRequest(
        session=session,
        prompt=prompt,
        available_tools=tool_registry.definitions(),
        context_window=context_window,
        assembled_context=runtime._assemble_provider_context(
            prompt=prompt,
            tool_results=(),
            session_metadata=session.metadata,
        ),
        metadata={**session.metadata, **({"provider_stream": True} if provider_stream else {})},
    )
    return session, request, tool_registry


def test_persist_event_assigns_db_sequence(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    _create_session_row(store, workspace=tmp_path, session_id="session-1")
    runtime = _runtime_with_store(tmp_path, store)
    coordinator = runtime._run_loop_coordinator

    envelope = coordinator._persist_event(
        session_id="session-1",
        event_type="runtime.tool_started",
        source="runtime",
        payload={"tool": "read_file"},
    )

    assert envelope.sequence == 1
    events = _loaded_events(store, workspace=tmp_path, session_id="session-1")
    assert [(event.sequence, event.event_type) for event in events] == [(1, "runtime.tool_started")]


def test_persist_events_batch_is_contiguous_and_deduped(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    _create_session_row(store, workspace=tmp_path, session_id="session-1")
    runtime = _runtime_with_store(tmp_path, store)
    coordinator = runtime._run_loop_coordinator

    envelopes = coordinator._persist_events(
        session_id="session-1",
        events=(
            ("graph.loop_step", "graph", {"step": 1}, None),
            ("graph.model_turn", "graph", {"turn": 1}, None),
            ("graph.response_ready", "graph", {"output_preview": "done"}, None),
        ),
    )

    assert [envelope.sequence for envelope in envelopes] == [1, 2, 3]
    # A duplicate delivery with the same dedupe key must not consume a sequence.
    deduped = coordinator._persist_events(
        session_id="session-1",
        events=(
            ("graph.loop_step", "graph", {"step": 1}, "loop-step-1"),
            ("graph.loop_step", "graph", {"step": 1}, "loop-step-1"),
        ),
    )
    assert [envelope.sequence for envelope in deduped] == [4]


def test_persist_chunk_reassigns_db_sequence(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    _create_session_row(store, workspace=tmp_path, session_id="session-1")
    runtime = _runtime_with_store(tmp_path, store)
    coordinator = runtime._run_loop_coordinator

    session = SessionState(
        session=SessionRef(id="session-1"),
        status="running",
        turn=1,
        metadata={},
    )
    chunk = RuntimeStreamChunk(
        kind="event",
        session=session,
        event=EventEnvelope(
            session_id="session-1",
            sequence=99,
            event_type="runtime.failed",
            source="runtime",
            payload={"error": "boom"},
        ),
    )

    persisted_chunk, sequence = coordinator._persist_chunk(chunk)

    assert persisted_chunk.event is not None and persisted_chunk.event.sequence == 1
    assert sequence == 1
    events = _loaded_events(store, workspace=tmp_path, session_id="session-1")
    assert [(event.sequence, event.event_type) for event in events] == [(1, "runtime.failed")]


def test_serialized_tool_results_roundtrip_through_checkpoint_reader() -> None:
    from voidcode.runtime.run_loop import _serialized_tool_results

    tool_results = [
        ToolResult(
            tool_name="read_file",
            status="ok",
            content="alpha\n",
            data={"tool_call_id": "call-1", "arguments": {"path": "a.txt"}},
        ),
        ToolResult(
            tool_name="shell_exec",
            status="error",
            error="boom",
            error_kind="tool_timeout",
            error_summary="timed out",
            error_details={"message": "timed out"},
            retry_guidance="retry",
            data={"tool_call_id": "call-2", "arguments": {"command": "ls"}},
        ),
    ]

    serialized = _serialized_tool_results(tool_results)

    assert serialized[0]["tool_name"] == "read_file"
    assert serialized[0]["status"] == "ok"
    assert serialized[0]["content"] == "alpha\n"
    assert serialized[0]["error"] is None
    assert serialized[1]["status"] == "error"
    assert serialized[1]["error"] == "boom"
    assert serialized[1]["error_kind"] == "tool_timeout"

    rehydrated = RuntimeResumeCoordinator.tool_results_from_checkpoint(list(serialized))
    assert [result.tool_name for result in rehydrated] == ["read_file", "shell_exec"]
    assert rehydrated[1].status == "error"
    assert rehydrated[1].error == "boom"
    assert rehydrated[1].error_kind == "tool_timeout"


def test_execute_graph_loop_streaming_dedupes_raw_provider_stream(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    _create_session_row(store, workspace=tmp_path, session_id="session-1")
    runtime = _runtime_with_store(tmp_path, store)

    class _StreamingGraph:
        def step(self, request: GraphRunRequest, tool_results: tuple, *, session: SessionState) -> _GraphStep:
            _ = request, tool_results, session
            raise AssertionError("streaming branch must not call step")

        def stream_step(self, request: GraphRunRequest, tool_results: tuple, *, session: SessionState):
            _ = request, tool_results, session
            yield GraphEvent(
                event_type="graph.provider_stream",
                source="graph",
                payload={"kind": "delta", "channel": "text", "text": "hello"},
            )
            yield _GraphStep(
                events=(GraphEvent(event_type="graph.response_ready", source="graph", payload={"output_preview": "done"}),),
                output="done",
                is_finished=True,
            )

    session, request, tool_registry = _graph_request(runtime, session_id="session-1", provider_stream=True)

    chunks = list(
        runtime._execute_graph_loop(
            graph=_StreamingGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            graph_request=request,
            tool_results=[],
        )
    )

    event_types = [chunk.event.event_type for chunk in chunks if chunk.event is not None]
    assert "graph.provider_stream" in event_types  # live client-only delta still streamed
    # The live raw provider_stream chunk is client-only: not persisted, not DB-sequenced.
    persisted = _loaded_events(store, workspace=tmp_path, session_id="session-1")
    assert [event.event_type for event in persisted] == ["graph.response_ready"]
    assert [event.sequence for event in persisted] == [1]
    # The renumbered batch yields the DB-assigned sequence.
    response_ready_chunk = next(chunk for chunk in chunks if chunk.event is not None and chunk.event.event_type == "graph.response_ready")
    assert response_ready_chunk.event is not None and response_ready_chunk.event.sequence == 1


def test_execute_graph_loop_captures_safe_boundary_checkpoint(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    _create_session_row(store, workspace=tmp_path, session_id="session-1")
    runtime = _runtime_with_store(tmp_path, store)
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\n", encoding="utf-8")

    class _ToolThenFinalGraph:
        def step(self, request: GraphRunRequest, tool_results: tuple, *, session: SessionState) -> _GraphStep:
            _ = request, session
            if not tool_results:
                return _GraphStep(
                    tool_call=ToolCall(tool_name="read_file", arguments={"path": str(sample_file)}),
                )
            return _GraphStep(output="done", is_finished=True)

        def is_at_safe_boundary(self) -> bool:
            return True

    session, request, tool_registry = _graph_request(runtime, session_id="session-1")

    chunks = list(
        runtime._execute_graph_loop(
            graph=_ToolThenFinalGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            graph_request=request,
            tool_results=[],
        )
    )

    assert any(chunk.kind == "output" and chunk.output == "done" for chunk in chunks)
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="session-1")
    assert checkpoint is not None
    assert checkpoint["kind"] == "interrupted"
    raw_tool_results = checkpoint["tool_results"]
    assert isinstance(raw_tool_results, list)
    assert [cast(dict[str, object], entry)["tool_name"] for entry in raw_tool_results] == ["read_file"]
    assert cast(int, checkpoint["last_event_sequence"]) > 0
