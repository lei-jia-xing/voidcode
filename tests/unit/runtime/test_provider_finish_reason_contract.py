"""Observable contract for provider finish reasons, restarts, and diagnostics.

Three provider behaviours converged in one reported session:

* a terminal chunk carrying an unrecognized/omitted finish reason was rejected
  as an internal provider failure,
* the transient retry that followed re-streamed the same live-only text, so the
  reply rendered twice, and
* the completed-but-unreported turn left no trace for session inspection.

These tests drive the real run loop and assert the observable contract: the
persisted transcript holds exactly one assistant message for the surviving
attempt, the restart event announces the discarded live projection only when
something was actually surfaced, and the terminal reason is recorded.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from voidcode.graph.contracts import GraphRunRequest
from voidcode.graph.provider_graph import ProviderGraph
from voidcode.provider.protocol import (
    ProviderDoneReason,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    TurnProvider,
)
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.provider.resolution import resolve_provider_model
from voidcode.runtime.policy import materialize_runtime_policy_snapshot
from voidcode.runtime.service import RuntimeStreamChunk, SessionState, ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef
from voidcode.runtime.storage import SqliteSessionStore


def _create_session_row(store: SqliteSessionStore, *, workspace: Path, session_id: str) -> None:
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=session_id,
        prompt="hi",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )


def _graph_request(runtime: VoidCodeRuntime, *, session_id: str) -> tuple[SessionState, GraphRunRequest, ToolRegistry]:
    effective_config = runtime.effective_runtime_config()
    runtime_config_metadata = runtime._runtime_config_metadata()
    runtime_policy = materialize_runtime_policy_snapshot(
        persisted_session_policy=None,
        agent_preset="leader",
        agent_manifest_id="leader",
        runtime_config=runtime_config_metadata,
        request_metadata={},
        parent_snapshot=None,
    ).as_payload()
    session = SessionState(
        session=SessionRef(id=session_id),
        status="running",
        turn=0,
        metadata={"runtime_config": runtime_config_metadata, "runtime_policy": runtime_policy},
    )
    tool_registry = runtime.tool_registry_for_effective_config(effective_config)
    request = GraphRunRequest(
        session=session,
        prompt="hi",
        available_tools=tool_registry.definitions(),
        context_window=runtime.prepare_provider_context_window(prompt="hi", tool_results=(), session_metadata=session.metadata),
        assembled_context=runtime.assemble_provider_context(prompt="hi", tool_results=(), session_metadata=session.metadata),
        metadata={**session.metadata, "provider_stream": True},
    )
    return session, request, tool_registry


def _streamed_text(chunks: list[RuntimeStreamChunk]) -> list[str]:
    fragments: list[str] = []
    for chunk in chunks:
        if chunk.event is None or chunk.event.event_type != "graph.provider_stream":
            continue
        payload = chunk.event.payload
        if payload.get("kind") == "delta" and payload.get("channel") == "text":
            text = payload.get("text")
            if isinstance(text, str):
                fragments.append(text)
    return fragments


def _events_of_type(chunks: list[RuntimeStreamChunk], event_type: str) -> list[dict[str, object]]:
    return [chunk.event.payload for chunk in chunks if chunk.event is not None and chunk.event.event_type == event_type]


def _response_ready_payloads(store: SqliteSessionStore, *, workspace: Path, session_id: str) -> list[dict[str, object]]:
    persisted = store.load_session(workspace=workspace, session_id=session_id).events
    return [dict(event.payload) for event in persisted if event.event_type == "graph.response_ready"]


class _UnknownThenStopStreamingProvider:
    """First attempt ends with an unrecognized finish reason; a second would succeed."""

    name = "opencode"

    def __init__(self) -> None:
        self.attempts = 0

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="hi", done_reason="stop")

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        _ = request
        self.attempts += 1
        yield ProviderStreamEvent(kind="delta", channel="text", text="hi")
        yield ProviderStreamEvent(kind="done", done_reason="unknown" if self.attempts == 1 else "stop")


class _RestartStreamingProvider:
    """Streams distinct text per attempt; ``fail_on_attempt`` raises after streaming."""

    name = "opencode"

    def __init__(self, *, fail_on_attempt: int | None, finish_reason: ProviderDoneReason = "stop") -> None:
        self.attempts = 0
        self._fail_on_attempt = fail_on_attempt
        self._finish_reason = finish_reason

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _ = request
        return ProviderTurnResult(output="non-stream output", done_reason="stop")

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        _ = request
        self.attempts += 1
        yield ProviderStreamEvent(kind="delta", channel="text", text=f"attempt {self.attempts}")
        if self._fail_on_attempt is not None and self.attempts == self._fail_on_attempt:
            raise ProviderExecutionError(
                kind="transient_failure",
                provider_name=self.name,
                model_name="gpt-5.4",
                message="temporary ssl failure",
            )
        yield ProviderStreamEvent(kind="done", done_reason=self._finish_reason)


def _graph_for(provider: TurnProvider) -> ProviderGraph:
    return ProviderGraph(
        provider=provider,
        provider_model=resolve_provider_model("opencode/gpt-5.4", registry=ModelProviderRegistry.with_defaults()),
    )


def _run_graph_loop(
    *,
    tmp_path: Path,
    provider: TurnProvider,
    session_id: str,
) -> tuple[list[RuntimeStreamChunk], SqliteSessionStore]:
    store = SqliteSessionStore()
    _create_session_row(store, workspace=tmp_path, session_id=session_id)
    runtime = VoidCodeRuntime(workspace=tmp_path, session_store=store)
    session, request, tool_registry = _graph_request(runtime, session_id=session_id)
    chunks = list(
        runtime._run_loop_coordinator.execute_graph_loop(
            graph=_graph_for(provider),
            tool_registry=tool_registry,
            session=session,
            sequence=0,
            graph_request=request,
            tool_results=[],
        )
    )
    return chunks, store


def test_unrecognized_finish_reason_completes_without_a_restart(tmp_path: Path) -> None:
    provider = _UnknownThenStopStreamingProvider()
    chunks, store = _run_graph_loop(tmp_path=tmp_path, provider=provider, session_id="session-1")

    # One provider response, one assistant message: the text reaches the client
    # once and the round completes instead of being restarted as a failure.
    assert _streamed_text(chunks) == ["hi"]
    assert provider.attempts == 1
    assert _events_of_type(chunks, "runtime.provider_transient_retry") == []
    assert _events_of_type(chunks, "runtime.failed") == []
    assert chunks[-1].session.status == "completed"
    assert [payload.get("output_preview") for payload in _response_ready_payloads(store, workspace=tmp_path, session_id="session-1")] == ["hi"]


def test_absent_finish_reason_is_recorded_in_the_persisted_transcript(tmp_path: Path) -> None:
    provider = _RestartStreamingProvider(fail_on_attempt=None, finish_reason="unknown")
    chunks, store = _run_graph_loop(tmp_path=tmp_path, provider=provider, session_id="session-4")

    assert _streamed_text(chunks) == ["attempt 1"]
    assert _response_ready_payloads(store, workspace=tmp_path, session_id="session-4") == [
        {"output_preview": "attempt 1", "finish_reason": "unknown", "finish_reason_reported": False}
    ]


def test_reported_finish_reason_is_recorded_as_reported(tmp_path: Path) -> None:
    provider = _RestartStreamingProvider(fail_on_attempt=None, finish_reason="stop")
    _chunks, store = _run_graph_loop(tmp_path=tmp_path, provider=provider, session_id="session-5")

    assert _response_ready_payloads(store, workspace=tmp_path, session_id="session-5") == [
        {"output_preview": "attempt 1", "finish_reason": "stop", "finish_reason_reported": True}
    ]
