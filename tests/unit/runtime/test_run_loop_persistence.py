from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

from tests.runtime_composition import save_checkpoint
from tests.runtime_storage import repositories_for_test_store
from voidcode.core.turns import FinalTurn, ResponseReadyFact, StreamFact, ToolTurn, TurnRequest
from voidcode.provider.protocol import ProviderStreamEvent
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.service import RuntimeRequest, RuntimeStreamChunk, SessionState, ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall
from voidcode.tools.read import ReadTool


def _create_session_row(store: SqliteSessionStore, *, workspace: Path, session_id: str) -> None:
    save_checkpoint(
        store,
        workspace=workspace,
        session_id=session_id,
        prompt="persistence probe",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )


def _runtime_with_store(
    tmp_path: Path,
    store: SqliteSessionStore,
    *,
    config: RuntimeConfig | None = None,
    tool_registry: ToolRegistry | None = None,
) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=tmp_path,
        config=config,
        repositories=repositories_for_test_store(store),
        tool_registry=tool_registry,
    )


def _loaded_events(store: SqliteSessionStore, *, workspace: Path, session_id: str) -> tuple[EventEnvelope, ...]:
    return store.load_session(workspace=workspace, session_id=session_id).events


def _turn_request(runtime: VoidCodeRuntime, *, session_id: str, provider_stream: bool = False) -> tuple[SessionState, TurnRequest, ToolRegistry]:
    run_id = f"test-run-{uuid4().hex}"
    runtime_request = RuntimeRequest(prompt="probe", session_id=session_id)
    prepared = runtime._prepare_stream_request_and_rehydration(
        runtime_request,
        session_id=session_id,
        run_id=run_id,
    )
    session = runtime._assemble_stream_session_metadata(prepared, run_id=run_id)
    tool_registry = runtime.tool_registry_for_effective_config(prepared.effective_config)
    prompt = runtime_request.prompt
    context_window = runtime.prepare_provider_context_window(
        prompt=prompt,
        tool_results=(),
        session_metadata=session.metadata,
    )
    request = TurnRequest(
        session=session,
        prompt=prompt,
        available_tools=tool_registry.definitions(),
        context_window=context_window,
        assembled_context=runtime.assemble_provider_context(
            prompt=prompt,
            tool_results=(),
            session_metadata=session.metadata,
        ),
        metadata={**session.metadata, **({"provider_stream": True} if provider_stream else {})},
        run_id=run_id,
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
        payload={"tool": "read"},
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


def test_execute_turn_engine_streaming_dedupes_raw_provider_stream(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = _runtime_with_store(tmp_path, store, config=RuntimeConfig(model="openai/gpt-4o"))

    class _StreamingGraph:
        def produce(self, request: TurnRequest, tool_results: tuple, *, session: SessionState) -> ToolTurn | FinalTurn:
            _ = request, tool_results, session
            raise AssertionError("streaming branch must not call step")

        def stream_produce(self, request: TurnRequest, tool_results: tuple, *, session: SessionState):
            _ = request, tool_results, session
            yield StreamFact(ProviderStreamEvent(kind="delta", channel="text", text="hello"))
            yield StreamFact(ProviderStreamEvent(kind="tool_call_start", tool_call_id="call-1", tool_name="read", tool_call_ordinal=0))
            yield StreamFact(ProviderStreamEvent(kind="tool_call_delta", tool_call_id="call-1", arguments_delta='{"path":'))
            yield StreamFact(ProviderStreamEvent(kind="tool_call_end", tool_call_id="call-1", parsed_arguments={"path": "sample.txt"}))
            yield FinalTurn(
                facts=(ResponseReadyFact("done"),),
                output="done",
            )

    session, request, tool_registry = _turn_request(runtime, session_id="session-1", provider_stream=True)

    chunks = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=_StreamingGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=[],
        )
    )

    event_types = [chunk.event.event_type for chunk in chunks if chunk.event is not None]
    assert [event_type for event_type in event_types if event_type.startswith("graph.tool_call_")] == [
        "graph.tool_call_start",
        "graph.tool_call_delta",
        "graph.tool_call_end",
    ]
    assert "graph.provider_stream" in event_types  # live client-only delta still streamed
    # The live raw provider_stream chunk is client-only: not persisted, not DB-sequenced.
    # Streaming turns additionally report the reasoning output diagnostic.
    persisted = _loaded_events(store, workspace=tmp_path, session_id="session-1")
    assert [event.event_type for event in persisted] == [
        "graph.response_ready",
        "runtime.reasoning_diagnostic",
    ]
    assert [event.sequence for event in persisted] == [1, 2]
    # The renumbered batch yields the DB-assigned sequence.
    response_ready_chunk = next(chunk for chunk in chunks if chunk.event is not None and chunk.event.event_type == "graph.response_ready")
    assert response_ready_chunk.event is not None and response_ready_chunk.event.sequence == 1


def test_execute_turn_engine_streaming_persists_aggregated_reasoning(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = _runtime_with_store(tmp_path, store, config=RuntimeConfig(model="openai/gpt-4o"))

    class _StreamingReasoningGraph:
        def produce(self, request: TurnRequest, tool_results: tuple, *, session: SessionState) -> ToolTurn | FinalTurn:
            _ = request, tool_results, session
            raise AssertionError("streaming branch must not call step")

        def stream_produce(self, request: TurnRequest, tool_results: tuple, *, session: SessionState):
            _ = request, tool_results, session
            yield StreamFact(ProviderStreamEvent(kind="delta", channel="reasoning", text="first thought "))
            yield StreamFact(ProviderStreamEvent(kind="delta", channel="reasoning", text="second thought"))
            yield FinalTurn(
                facts=(ResponseReadyFact("done"),),
                output="done",
            )

    session, request, tool_registry = _turn_request(runtime, session_id="session-1", provider_stream=True)

    chunks = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=_StreamingReasoningGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=[],
        )
    )

    # Live deltas still stream to the client...
    event_types = [chunk.event.event_type for chunk in chunks if chunk.event is not None]
    assert "graph.provider_stream" in event_types
    # ...but are not persisted: the turn persists one aggregated reasoning_part
    # so replay of a completed session keeps the thinking.
    persisted = _loaded_events(store, workspace=tmp_path, session_id="session-1")
    persisted_types = [event.event_type for event in persisted]
    assert "graph.provider_stream" not in persisted_types
    assert persisted_types.count("runtime.reasoning_part") == 1
    # The aggregated reasoning part lands before the terminal response, with the
    # reasoning output diagnostic after it (same tail as non-streaming turns).
    assert persisted_types[-2:] == [
        "graph.response_ready",
        "runtime.reasoning_diagnostic",
    ]
    reasoning = next(event for event in persisted if event.event_type == "runtime.reasoning_part")
    assert reasoning.payload["text"] == "first thought second thought"
    assert reasoning.payload["preview"] == "first thought second thought"
    assert reasoning.payload["source"] == "provider_stream"


def test_execute_turn_engine_non_streaming_persists_step_reasoning(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = _runtime_with_store(tmp_path, store, config=RuntimeConfig(model="openai/gpt-4o"))

    class _NonStreamingReasoningGraph:
        def produce(self, request: TurnRequest, tool_results: tuple, *, session: SessionState) -> ToolTurn | FinalTurn:
            _ = request, tool_results, session
            return FinalTurn(
                facts=(ResponseReadyFact("done"),),
                output="done",
                reasoning="background child thought",
            )

        def stream_produce(self, request: TurnRequest, tool_results: tuple, *, session: SessionState):
            _ = request, tool_results, session
            raise AssertionError("non-streaming branch must not call stream_step")

    session, request, tool_registry = _turn_request(runtime, session_id="session-1")

    chunks = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=_NonStreamingReasoningGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=[],
        )
    )

    # No live provider_stream deltas on the non-streaming path: the turn persists
    # one aggregated runtime.reasoning_part so replay keeps the child's thinking.
    event_types = [chunk.event.event_type for chunk in chunks if chunk.event is not None]
    assert "graph.provider_stream" not in event_types
    persisted = _loaded_events(store, workspace=tmp_path, session_id="session-1")
    persisted_types = [event.event_type for event in persisted]
    assert persisted_types.count("runtime.reasoning_part") == 1
    assert persisted_types[-2:] == [
        "graph.response_ready",
        "runtime.reasoning_diagnostic",
    ]
    reasoning = next(event for event in persisted if event.event_type == "runtime.reasoning_part")
    assert reasoning.payload["text"] == "background child thought"
    diagnostic = next(event for event in persisted if event.event_type == "runtime.reasoning_diagnostic")
    assert diagnostic.payload["reasoning_output_observed"] is True
    assert diagnostic.payload["captured_part_count"] == 1
    assert diagnostic.payload["captured_text_char_count"] == len("background child thought")


def test_execute_turn_engine_captures_safe_boundary_checkpoint(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    runtime = _runtime_with_store(
        tmp_path,
        store,
        config=RuntimeConfig(model="openai/gpt-4o"),
        tool_registry=ToolRegistry.from_tools((ReadTool(),)),
    )
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\n", encoding="utf-8")

    class _ToolThenFinalGraph:
        def produce(self, request: TurnRequest, tool_results: tuple, *, session: SessionState) -> ToolTurn | FinalTurn:
            _ = request, session
            if not tool_results:
                return ToolTurn(calls=(ToolCall(tool_name="read", arguments={"path": str(sample_file)}),))
            return FinalTurn(output="done")

        def is_at_safe_boundary(self) -> bool:
            return True

    session, request, tool_registry = _turn_request(runtime, session_id="session-1")

    chunks = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=_ToolThenFinalGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=[],
        )
    )

    assert any(chunk.kind == "output" and chunk.output == "done" for chunk in chunks)
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="session-1")
    assert checkpoint is not None
    assert checkpoint["kind"] == "interrupted"
    raw_tool_results = checkpoint["tool_results"]
    assert isinstance(raw_tool_results, list)
    assert [cast(dict[str, object], entry)["tool_name"] for entry in raw_tool_results] == ["read"]
    assert cast(int, checkpoint["last_event_sequence"]) > 0
