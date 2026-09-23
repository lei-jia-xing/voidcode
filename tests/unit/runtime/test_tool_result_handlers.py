from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.graph.contracts import GraphRunRequest
from voidcode.hook.typed import (
    RewriteResult,
    ToolResultHandlerBinding,
    ToolResultHandlerDecision,
    ToolResultHandlerRegistry,
)
from voidcode.runtime.config import RuntimeConfig, RuntimeHooksConfig, RuntimeMcpConfig
from voidcode.runtime.context.window import ToolResultView
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
    reasoning: str | None = None


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
        return RewriteResult(content="provider summary")

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
        return RewriteResult(content="provider error summary", error="provider error")

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
        return RewriteResult(content="same provider view")

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
        return RewriteResult(content="rewritten")

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


def test_builtin_truncation_keeps_stored_truth_intact() -> None:
    from voidcode.hook.typed import builtin_tool_result_handler_registry

    source = ToolResult(tool_name="capture", status="ok", content="x" * 20000)
    snapshot = ToolResult(tool_name="capture", status="ok", content="x" * 20000)
    registry = builtin_tool_result_handler_registry()

    outcome = registry.apply(result=ToolResultView(result=source, content=source.content))

    assert outcome.action == "rewrite"
    assert outcome.view.content is not None
    assert len(outcome.view.content) < len(snapshot.content or "")
    assert "truncated" in outcome.view.content
    assert source == snapshot

    small = ToolResult(tool_name="capture", status="ok", content="small")
    small_outcome = registry.apply(result=ToolResultView(result=small, content=small.content))
    assert small_outcome.action == "unchanged"
    assert small_outcome.view.content == "small"


def test_builtin_truncation_chain_is_last_wins() -> None:
    from voidcode.hook.typed import (
        ToolResultHandlerBinding,
        builtin_tool_result_handler_registry,
        compose_tool_result_handler_registry,
    )

    def override(result: ToolResultView) -> ToolResultHandlerDecision:
        _ = result
        return RewriteResult(content="override")

    builtin = builtin_tool_result_handler_registry().bindings
    registry = compose_tool_result_handler_registry(
        builtin,
        (ToolResultHandlerBinding(name="override", handler=override, priority=1),),
    )
    source = ToolResult(tool_name="capture", status="ok", content="y" * 20000)
    outcome = registry.apply(result=ToolResultView(result=source, content=source.content))

    assert outcome.view.content == "override"
    assert source.content == "y" * 20000


def test_tool_result_view_isolates_authoritative_result_and_data() -> None:
    source = ToolResult(tool_name="capture", status="ok", content="source", data={"nested": {"secret": "value"}})
    view = ToolResultView(result=source, content=source.content)

    view.result.data["nested"]["secret"] = "result mutation"  # type: ignore[index]
    view.data["nested"]["secret"] = "provider mutation"  # type: ignore[index]
    view.data["added"] = True

    assert source.data == {"nested": {"secret": "value"}}
    assert view.result.data == {"nested": {"secret": "result mutation"}}


def _invoke_runtime(
    workspace: Path,
    *,
    hooks: RuntimeHooksConfig,
) -> tuple[VoidCodeRuntime, _ObserveGraph]:
    """Drive an ``invoke_tool`` dispatch so the inner pre_tool hook runs."""
    tool = _ResultTool(ToolResult(tool_name="capture", status="ok", content="source"))
    graph = _ObserveGraph(ToolCall(tool_name="invoke_tool", arguments={"name": "capture", "arguments": {}}))
    runtime = VoidCodeRuntime(
        workspace=workspace,
        tool_registry=ToolRegistry.from_tools(cast(Any, [InvokeTool(), tool])),
        graph=cast(Any, graph),
        config=RuntimeConfig(
            execution_engine="deterministic",
            approval_mode="allow",
            mcp=RuntimeMcpConfig(enabled=False),
            hooks=hooks,
        ),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    return runtime, graph


def test_invoke_dispatch_pre_tool_hook_failure_honors_failure_mode(tmp_path: Path) -> None:
    """invoke_tool dispatch must escalate `fail` like the primary pre_tool path."""
    crash = (sys.executable, "-c", "raise SystemExit(5)")

    warn_runtime, _ = _invoke_runtime(
        tmp_path / "warn",
        hooks=RuntimeHooksConfig(enabled=True, failure_mode="warn", pre_tool=(crash,)),
    )
    warn_response = warn_runtime.run(RuntimeRequest(prompt="run", session_id="invoke-warn"))

    # warn: the dispatch degrades to model-visible tool feedback, run continues.
    assert warn_response.session.status == "completed"
    completed = [event for event in warn_response.events if event.event_type == "runtime.tool_completed"]
    assert completed
    failure = completed[-1].payload
    assert failure["tool"] == "capture"
    assert failure["status"] == "error"
    assert "tool pre-hook failed" in str(failure["content"])

    fail_runtime, _ = _invoke_runtime(
        tmp_path / "fail",
        hooks=RuntimeHooksConfig(enabled=True, failure_mode="fail", pre_tool=(crash,)),
    )
    with pytest.raises(RuntimeError, match="pre-hook failed"):
        _ = fail_runtime.run(RuntimeRequest(prompt="run", session_id="invoke-fail"))


def test_invoke_dispatch_pre_tool_cancel_stays_tool_feedback_in_both_modes(tmp_path: Path) -> None:
    """Deliberate asymmetry vs executor failure: a hook cancel on the inner dispatch
    yields model-visible tool feedback and the run continues, even under `fail`.
    """
    cancel = (sys.executable, "-c", 'print(\'{"action": "cancel", "diagnostic": "operator_hold"}\')')

    for mode in ("warn", "fail"):
        (tmp_path / mode).mkdir()
        runtime, _ = _invoke_runtime(
            tmp_path / mode,
            hooks=RuntimeHooksConfig(enabled=True, failure_mode=mode, pre_tool=(cancel,)),
        )
        response = runtime.run(RuntimeRequest(prompt="run", session_id=f"invoke-cancel-{mode}"))

        # Cancel surfaces as model-visible tool feedback, not a run failure.
        assert response.session.status == "completed"
        completed = [event for event in response.events if event.event_type == "runtime.tool_completed"]
        assert completed
        feedback = completed[-1].payload
        assert feedback["tool"] == "capture"
        assert feedback["status"] == "error"
        assert feedback["error"] == "tool 'capture' blocked: operator_hold"
        assert feedback["diagnostics"]["kind"] == "hook_cancelled"
