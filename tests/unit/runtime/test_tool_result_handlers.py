from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from voidcode.graph.contracts import GraphRunRequest
from voidcode.hook.typed import (
    ToolResultHandlerBinding,
    ToolResultHandlerDecision,
    ToolResultHandlerRegistry,
)
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.context_window import ToolResultView
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.tool_registry import ToolRegistry
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult
from voidcode.tools.invoke_tool import InvokeTool


@dataclass(frozen=True, slots=True)
class _Step:
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False


class _ObserveGraph:
    def __init__(self, call: ToolCall) -> None:
        self.call = call
        self.provider_views: list[tuple[str | None, str | None]] = []

    def step(self, request: GraphRunRequest, tool_results: tuple[Any, ...], *, session: object) -> _Step:
        _ = session
        if tool_results:
            context_results = request.context_window.tool_results if request.context_window is not None else ()
            assembled_results = request.assembled_context.tool_results
            self.provider_views.append(
                (
                    context_results[-1].content if context_results else None,
                    assembled_results[-1].content if assembled_results else None,
                )
            )
            return _Step(output="done", is_finished=True)
        return _Step(tool_call=self.call)


class _ResultTool:
    definition = ToolDefinition(name="capture", description="capture output", read_only=True)

    def __init__(self, result: ToolResult) -> None:
        self.result = result

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        return self.result


def _runtime(
    workspace: Path,
    *,
    call: ToolCall,
    result: ToolResult,
    handler: object,
    include_invoke_tool: bool = False,
) -> tuple[VoidCodeRuntime, _ObserveGraph]:
    tool = _ResultTool(result)
    tools: list[object] = [tool]
    if include_invoke_tool:
        tools.insert(0, InvokeTool())
    graph = _ObserveGraph(call)
    runtime = VoidCodeRuntime(
        workspace=workspace,
        tool_registry=ToolRegistry.from_tools(cast(Any, tools)),
        graph=cast(Any, graph),
        config=RuntimeConfig(execution_engine="deterministic", approval_mode="allow", mcp=RuntimeMcpConfig(enabled=False)),
        permission_policy=PermissionPolicy(mode="allow"),
        tool_result_handler_registry=ToolResultHandlerRegistry((ToolResultHandlerBinding("rewrite", cast(Any, handler)),)),
    )
    return runtime, graph


def test_result_handler_changes_both_provider_surfaces_but_not_authority(tmp_path: Path) -> None:
    source = ToolResult(tool_name="capture", status="ok", content="source")

    def rewrite(result: ToolResultView) -> ToolResultHandlerDecision:
        assert result.content == "source"
        return ToolResultHandlerDecision(action="rewrite", content="provider summary")

    runtime, graph = _runtime(
        tmp_path,
        call=ToolCall(tool_name="capture", arguments={"value": "x"}),
        result=source,
        handler=rewrite,
    )
    response = runtime.run(RuntimeRequest(prompt="run", session_id="result-view-success"))

    assert response.session.status == "completed"
    assert graph.provider_views == [("provider summary", "provider summary")]
    completed = next(event for event in response.events if event.event_type == "runtime.tool_completed")
    assert completed.payload["content"] == "source"
    stored = runtime._session_store.load_session(workspace=tmp_path, session_id="result-view-success")
    stored_completed = next(event for event in stored.events if event.event_type == "runtime.tool_completed")
    assert stored_completed.payload["content"] == "source"
    checkpoint = runtime._session_store.load_resume_checkpoint(workspace=tmp_path, session_id="result-view-success")
    assert checkpoint is not None
    checkpoint_results = checkpoint["tool_results"]
    assert isinstance(checkpoint_results, list)
    assert checkpoint_results[0]["content"] == "source"
    provenance = response.session.metadata["tool_result_handlers"]
    assert isinstance(provenance, dict)
    assert "provider summary" not in str(provenance)


def test_result_handler_rewrites_error_view_without_changing_error_authority(tmp_path: Path) -> None:
    source = ToolResult(tool_name="capture", status="error", content="source failure", error="source failure")

    def rewrite(result: ToolResultView) -> ToolResultHandlerDecision:
        assert result.status == "error"
        return ToolResultHandlerDecision(action="rewrite", content="provider error summary", error="provider error")

    runtime, graph = _runtime(
        tmp_path,
        call=ToolCall(tool_name="capture", arguments={}),
        result=source,
        handler=rewrite,
    )
    response = runtime.run(RuntimeRequest(prompt="run", session_id="result-view-error"))

    assert response.session.status == "completed"
    assert graph.provider_views == [("provider error summary", "provider error summary")]
    completed = next(event for event in response.events if event.event_type == "runtime.tool_completed")
    assert completed.payload["content"] == "source failure"
    assert completed.payload["error"] == "source failure"


def test_native_and_invoke_inner_use_the_same_result_view_projection(tmp_path: Path) -> None:
    seen: list[str] = []

    def rewrite(result: ToolResultView) -> ToolResultHandlerDecision:
        seen.append(result.tool_name)
        return ToolResultHandlerDecision(action="rewrite", content="same provider view")

    native_runtime, native_graph = _runtime(
        tmp_path / "native",
        call=ToolCall(tool_name="capture", arguments={}),
        result=ToolResult(tool_name="capture", status="ok", content="source"),
        handler=rewrite,
    )
    invoke_runtime, invoke_graph = _runtime(
        tmp_path / "invoke",
        call=ToolCall(tool_name="invoke_tool", arguments={"name": "capture", "arguments": {}}),
        result=ToolResult(tool_name="capture", status="ok", content="source"),
        handler=rewrite,
        include_invoke_tool=True,
    )

    native_runtime.run(RuntimeRequest(prompt="native", session_id="native-result-view"))
    invoke_runtime.run(RuntimeRequest(prompt="invoke", session_id="invoke-result-view"))

    assert native_graph.provider_views == [("same provider view", "same provider view")]
    assert invoke_graph.provider_views == [("same provider view", "same provider view")]
    assert seen == ["capture", "capture"]


def test_replayed_results_are_passed_through_without_handler_execution(tmp_path: Path) -> None:
    calls: list[str] = []

    def rewrite(result: ToolResultView) -> ToolResultHandlerDecision:
        calls.append(result.tool_name)
        return ToolResultHandlerDecision(action="rewrite", content="rewritten")

    runtime, _graph = _runtime(
        tmp_path,
        call=ToolCall(tool_name="capture", arguments={}),
        result=ToolResult(tool_name="capture", status="ok", content="source", source="replayed_conversation"),
        handler=rewrite,
    )
    projected, provenance = runtime._run_loop_coordinator._provider_tool_results(
        tool_results=[ToolResult(tool_name="capture", status="ok", content="source", source="replayed_conversation")]
    )

    checkpoint_result = ToolResult(tool_name="capture", status="ok", content="checkpoint")
    projected_checkpoint, checkpoint_provenance = runtime._run_loop_coordinator._provider_tool_results(
        tool_results=[checkpoint_result],
        skip_result_count=1,
    )
    assert projected_checkpoint[0] is checkpoint_result
    assert checkpoint_provenance is None
    assert projected[0].content == "source"
    assert provenance is None
    assert calls == []


def test_result_handler_failure_falls_back_to_source_and_records_bounded_provenance(tmp_path: Path) -> None:
    def broken(result: object) -> ToolResultHandlerDecision:
        _ = result
        raise RuntimeError("raw failure must not escape")

    runtime, graph = _runtime(
        tmp_path,
        call=ToolCall(tool_name="capture", arguments={}),
        result=ToolResult(tool_name="capture", status="ok", content="source"),
        handler=broken,
    )
    response = runtime.run(RuntimeRequest(prompt="run", session_id="result-view-failure"))

    assert graph.provider_views == [("source", "source")]
    provenance = response.session.metadata["tool_result_handlers"]
    assert isinstance(provenance, dict)
    entries = provenance["results"]
    assert isinstance(entries, list)
    assert entries[0]["action"] == "error"
    assert entries[0]["failure"] == "handler 'rewrite' failed"
    assert "raw failure" not in str(provenance)
