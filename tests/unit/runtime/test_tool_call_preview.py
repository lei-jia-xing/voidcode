from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from voidcode.graph.contracts import GraphEvent, GraphRunRequest
from voidcode.graph.provider_graph import ProviderGraph
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.runtime.provider_protocol import ProviderStreamEvent, ProviderTurnRequest, ProviderTurnResult
from voidcode.runtime.service import SessionState, ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.tool_call_preview import (
    PREVIEW_DIFF_MAX_CHARS,
    PREVIEW_SNAPSHOT_MAX_BYTES,
    build_partial_tool_call_preview,
    build_tool_call_preview,
)
from voidcode.tools.contracts import ToolCall


@dataclass(frozen=True, slots=True)
class _GraphStep:
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False


def _session_and_request(runtime: VoidCodeRuntime, *, session_id: str) -> tuple[SessionState, GraphRunRequest, ToolRegistry]:
    session = SessionState(
        session=SessionRef(id=session_id),
        status="running",
        turn=0,
        metadata={"runtime_config": runtime._runtime_config_metadata()},
    )
    prompt = "preview probe"
    tool_registry = runtime.tool_registry_for_effective_config(runtime.effective_runtime_config())
    context_window = runtime.prepare_provider_context_window(prompt=prompt, tool_results=(), session_metadata=session.metadata)
    request = GraphRunRequest(
        session=session,
        prompt=prompt,
        available_tools=tool_registry.definitions(),
        context_window=context_window,
        assembled_context=runtime.assemble_provider_context(
            prompt=prompt,
            tool_results=(),
            session_metadata=session.metadata,
        ),
        metadata={"provider_stream": True},
    )
    return session, request, tool_registry


def test_partial_write_preview_reads_snapshot_without_mutation(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    raw_before = target.read_bytes()

    preview = build_partial_tool_call_preview(
        workspace=tmp_path,
        tool_name="write",
        argument_text='{"path":"sample.txt","content":"new',
    )

    assert preview is not None
    assert preview["status"] == "ready"
    assert preview["phase"] == "partial"
    assert preview["live_only"] is True
    assert "+new" in preview["diff"]
    assert target.read_bytes() == raw_before


def test_partial_edit_preview_has_bounded_diff_and_final_reconciliation(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    partial = build_partial_tool_call_preview(
        workspace=tmp_path,
        tool_name="edit",
        argument_text='{"path":"sample.txt","oldString":"old","newString":"new',
    )
    final = build_tool_call_preview(
        workspace=tmp_path,
        tool_name="edit",
        arguments={"path": "sample.txt", "oldString": "old", "newString": "new\n"},
        phase="final",
    )

    assert partial is not None and final is not None
    assert partial["status"] == "ready"
    assert partial["live_only"] is True
    assert final["phase"] == "final"
    assert final["live_only"] is False
    assert final["diff"] != partial["diff"]
    assert "+new" in final["diff"]
    assert target.read_text(encoding="utf-8") == "old\n"
    assert len(final["diff"].encode("utf-8")) <= PREVIEW_DIFF_MAX_CHARS


def test_preview_unsafe_missing_and_oversized_inputs_are_degraded(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old\n", encoding="utf-8")

    unsafe = build_tool_call_preview(
        workspace=tmp_path,
        tool_name="write",
        arguments={"path": "../outside.txt", "content": "secret"},
    )
    missing = build_partial_tool_call_preview(
        workspace=tmp_path,
        tool_name="edit",
        argument_text='{"content":"new"}',
    )
    oversized = build_tool_call_preview(
        workspace=tmp_path,
        tool_name="write",
        arguments={"path": "sample.txt", "content": "x" * (PREVIEW_SNAPSHOT_MAX_BYTES + 1)},
    )

    assert unsafe is not None and unsafe["status"] == "degraded" and unsafe["reason"] == "unsafe_path"
    assert missing is not None and missing["status"] == "degraded" and missing["reason"] == "missing_path"
    assert oversized is not None and oversized["status"] == "degraded"
    assert oversized["reason"] == "proposed_content_too_large"


def test_streaming_write_diff_preview_is_live_only_and_not_persisted(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    session_id = "preview-session"
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=session_id,
        prompt="preview probe",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=store)
    session, request, tool_registry = _session_and_request(runtime, session_id=session_id)
    target = tmp_path / "sample.txt"
    target.write_text("old\n", encoding="utf-8")

    class _StreamingWriteGraph:
        def stream_step(self, request: GraphRunRequest, tool_results: tuple, *, session: SessionState):
            _ = request, tool_results, session
            yield GraphEvent(
                event_type="graph.tool_call_start",
                source="graph",
                payload={"kind": "tool_call_start", "tool_call_id": "call-1", "tool_name": "write"},
            )
            yield GraphEvent(
                event_type="graph.tool_call_delta",
                source="graph",
                payload={
                    "kind": "tool_call_delta",
                    "tool_call_id": "call-1",
                    "tool_name": "write",
                    "arguments_delta": '{"path":"sample.txt","content":"new',
                },
            )
            yield GraphEvent(
                event_type="graph.tool_call_end",
                source="graph",
                payload={
                    "kind": "tool_call_end",
                    "tool_call_id": "call-1",
                    "tool_name": "write",
                    "parsed_arguments": {"path": "sample.txt", "content": "new\n"},
                },
            )
            yield _GraphStep(
                events=(GraphEvent(event_type="graph.response_ready", source="graph", payload={"output_preview": "done"}),),
                output="done",
                is_finished=True,
            )

    chunks = list(
        runtime._run_loop_coordinator.execute_graph_loop(
            graph=_StreamingWriteGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            graph_request=request,
            tool_results=[],
        )
    )
    lifecycle = [chunk.event for chunk in chunks if chunk.event is not None and chunk.event.event_type.startswith("graph.tool_call_")]
    assert len(lifecycle) == 3
    for event in lifecycle:
        assert event is not None
        assert "arguments_delta" not in event.payload
        assert "parsed_arguments" not in event.payload
        diff_preview = event.payload["diff_preview"]
        assert isinstance(diff_preview, dict)
        assert diff_preview["live_only"] is True
    assert "+new" in lifecycle[-1].payload["diff_preview"]["diff"]
    assert target.read_text(encoding="utf-8") == "old\n"
    persisted = store.load_session(workspace=tmp_path, session_id=session_id).events
    assert not any(event.event_type.startswith("graph.tool_call_") for event in persisted)
    assert not any("diff_preview" in event.payload for event in persisted)


def test_streaming_non_write_lifecycle_keeps_existing_argument_fields(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    session_id = "read-preview-session"
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=session_id,
        prompt="preview probe",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=store)
    session, request, tool_registry = _session_and_request(runtime, session_id=session_id)

    class _StreamingReadGraph:
        def stream_step(self, request: GraphRunRequest, tool_results: tuple, *, session: SessionState):
            _ = request, tool_results, session
            yield GraphEvent(
                event_type="graph.tool_call_delta",
                source="graph",
                payload={
                    "kind": "tool_call_delta",
                    "tool_call_id": "call-read",
                    "tool_name": "read",
                    "arguments_delta": '{"path":"sample.txt"}',
                },
            )
            yield _GraphStep(
                events=(GraphEvent(event_type="graph.response_ready", source="graph", payload={"output_preview": "done"}),),
                output="done",
                is_finished=True,
            )

    chunks = list(
        runtime._run_loop_coordinator.execute_graph_loop(
            graph=_StreamingReadGraph(),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            graph_request=request,
            tool_results=[],
        )
    )
    delta = next(chunk.event for chunk in chunks if chunk.event is not None and chunk.event.event_type == "graph.tool_call_delta")
    assert delta is not None
    assert delta.payload["arguments_delta"] == '{"path":"sample.txt"}'
    assert "diff_preview" not in delta.payload


def test_final_tool_request_carries_non_live_diff_preview(tmp_path: Path) -> None:
    store = SqliteSessionStore()
    session_id = "final-preview-session"
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=session_id,
        prompt="preview probe",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=store)
    session, _request, tool_registry = _session_and_request(runtime, session_id=session_id)
    graph_step = _GraphStep(
        tool_call=ToolCall(
            tool_name="write",
            tool_call_id="call-final",
            arguments={"path": "new.txt", "content": "new content\n"},
        )
    )

    first_chunk = next(
        runtime._run_loop_coordinator._plan_tool_step(
            session=session,
            sequence=0,
            tool_registry=tool_registry,
            graph_step=graph_step,
        )
    )

    assert first_chunk.event is not None
    assert first_chunk.event.event_type == "graph.tool_request_created"
    diff_preview = first_chunk.event.payload["diff_preview"]
    assert isinstance(diff_preview, dict)
    assert diff_preview["phase"] == "final"
    assert diff_preview["live_only"] is False
    assert diff_preview["status"] == "ready"
    assert "+new content" in diff_preview["diff"]
    assert not (tmp_path / "new.txt").exists()


def test_provider_graph_binds_workspace_preview_callback_and_sanitizes_write_events(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old\n", encoding="utf-8")
    runtime = VoidCodeRuntime(workspace=tmp_path)
    session, request, _tool_registry = _session_and_request(runtime, session_id="provider-preview-session")
    callback_inputs: list[tuple[str, tuple[str, ...]]] = []

    def preview_callback(tool_name: str, fragments: tuple[str, ...], parsed: dict[str, object] | None) -> dict[str, object] | None:
        _ = parsed
        callback_inputs.append((tool_name, fragments))
        return build_partial_tool_call_preview(
            workspace=tmp_path,
            tool_name=tool_name,
            argument_text="".join(fragments),
        )

    class _Provider:
        name = "opencode"

        def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            _ = request
            return ProviderTurnResult(output="unused")

        def stream_turn(self, request: ProviderTurnRequest):
            _ = request
            yield ProviderStreamEvent(kind="tool_call_start", channel="tool", tool_call_id="call-1", tool_name="write")
            yield ProviderStreamEvent(
                kind="tool_call_delta",
                channel="tool",
                tool_call_id="call-1",
                tool_name="write",
                arguments_delta='{"path":"sample.txt","content":"new',
            )
            yield ProviderStreamEvent(
                kind="tool_call_end",
                channel="tool",
                tool_call_id="call-1",
                tool_name="write",
                parsed_arguments={"path": "sample.txt", "content": "new\n"},
            )
            yield ProviderStreamEvent(kind="done", channel="tool", done_reason="tool_calls")

    graph = ProviderGraph(
        provider=_Provider(),
        provider_model=resolve_provider_model("opencode/gpt-5.4", registry=ModelProviderRegistry.with_defaults()),
    )
    items = list(
        graph.stream_step(
            request=GraphRunRequest(
                session=session,
                prompt=request.prompt,
                assembled_context=request.assembled_context,
                available_tools=request.available_tools,
                context_window=request.context_window,
                metadata={"provider_stream": True},
                tool_call_preview=preview_callback,
            ),
            tool_results=(),
            session=session,
        )
    )
    lifecycle = [item for item in items if isinstance(item, GraphEvent) and item.event_type.startswith("graph.tool_call_")]
    assert len(lifecycle) == 3
    assert len(callback_inputs) == 3
    assert all("arguments_delta" not in event.payload and "parsed_arguments" not in event.payload for event in lifecycle)
    assert all(isinstance(event.payload.get("diff_preview"), dict) for event in lifecycle)
    assert "+new" in lifecycle[-1].payload["diff_preview"]["diff"]
    assert target.read_text(encoding="utf-8") == "old\n"
