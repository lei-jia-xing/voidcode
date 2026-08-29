from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from voidcode.graph.contracts import GraphEvent, GraphRunRequest
from voidcode.hook.typed import ToolInputDecision, ToolInputEvent, ToolInputHandlerBinding, ToolInputHandlerRegistry
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.tool_registry import ToolRegistry
from voidcode.tools.contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult
from voidcode.tools.invoke_tool import InvokeTool


@dataclass(frozen=True, slots=True)
class _Step:
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False
    events: tuple[GraphEvent, ...] = ()


class _OneToolGraph:
    def __init__(self, call: ToolCall) -> None:
        self.call = call

    def step(self, request: GraphRunRequest, tool_results: tuple[ToolResult, ...], *, session: object) -> _Step:
        _ = request, session
        return _Step(tool_call=self.call) if not tool_results else _Step(output="done", is_finished=True)


class _CaptureTool:
    definition = ToolDefinition(
        name="capture",
        description="capture canonicalized input",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        read_only=False,
    )

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[ToolCall] = []
        self.failure = failure

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        self.calls.append(call)
        if self.failure is not None:
            raise self.failure
        return ToolResult(tool_name=call.tool_name, status="ok", content=str(call.arguments["path"]))


def _runtime(
    workspace: Path,
    tool: _CaptureTool,
    registry: ToolInputHandlerRegistry,
    *,
    approval_mode: Literal["allow", "ask"] = "allow",
    initial_call: ToolCall | None = None,
    include_invoke_tool: bool = False,
) -> VoidCodeRuntime:
    tools = [tool]
    if include_invoke_tool:
        tools.insert(0, InvokeTool())
    return VoidCodeRuntime(
        workspace=workspace,
        tool_registry=ToolRegistry.from_tools(tools),
        graph=_OneToolGraph(initial_call or ToolCall(tool_name="capture", arguments={"path": "./input.txt"})),
        config=RuntimeConfig(execution_engine="deterministic", approval_mode=approval_mode, mcp=RuntimeMcpConfig(enabled=False)),
        permission_policy=PermissionPolicy(mode=approval_mode),
        tool_input_handler_registry=registry,
    )


def _canonicalizer(path: str, calls: list[str] | None = None):
    def canonicalize(event: ToolInputEvent) -> ToolInputDecision:
        if calls is not None:
            calls.append(event.tool_call.tool_name)
        return ToolInputDecision(action="rewrite", arguments={"path": path})

    return canonicalize


def _blocker(reason: str, calls: list[str] | None = None):
    def block(event: ToolInputEvent) -> ToolInputDecision:
        if calls is not None:
            calls.append(event.tool_call.tool_name)
        return ToolInputDecision(action="block", reason=reason)

    return block


def test_typed_rewrite_preserves_raw_graph_args_and_uses_final_execution_args(tmp_path: Path) -> None:
    tool = _CaptureTool()
    registry = ToolInputHandlerRegistry((ToolInputHandlerBinding("canonicalize", _canonicalizer("canonical.txt")),))
    response = _runtime(tmp_path, tool, registry).run(RuntimeRequest(prompt="capture"))
    assert response.session.status == "completed"
    assert [call.arguments for call in tool.calls] == [{"path": "canonical.txt"}]
    graph_request = next(event for event in response.events if event.event_type == "graph.tool_request_created")
    assert graph_request.payload["arguments"] == {"path": "./input.txt"}
    types = [event.event_type for event in response.events]
    assert types.index("graph.tool_request_created") < types.index("runtime.tool_lookup_succeeded") < types.index("runtime.tool_input_processed")
    trace = next(event for event in response.events if event.event_type == "runtime.tool_input_processed")
    assert trace.payload["surface"] == "typed_input"
    assert trace.payload["hook_status"] == "ok"
    assert isinstance(trace.payload["policy"], dict)
    assert trace.payload["policy"]["mode"] == "normal"
    metadata = trace.payload["rewrite"]
    assert isinstance(metadata, dict)
    assert metadata["original_sha256"] != metadata["final_sha256"]
    started = next(event for event in response.events if event.event_type == "runtime.tool_started")
    completed = next(event for event in response.events if event.event_type == "runtime.tool_completed")
    assert isinstance(started.payload["display"], dict)
    assert started.payload["display"]["args"] == ["canonical.txt"]
    assert completed.payload["arguments"] == {"path": "canonical.txt"}
    event_sequences = [event.sequence for event in response.events]
    assert event_sequences == list(range(1, len(event_sequences) + 1))
    assert started.sequence < completed.sequence


def test_inner_invoke_enforces_delegated_child_policy_before_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _CaptureTool()
    runtime = _runtime(
        tmp_path,
        tool,
        ToolInputHandlerRegistry(()),
        initial_call=ToolCall(
            tool_name="invoke_tool",
            arguments={"name": "capture", "arguments": {"path": "inner.txt"}},
        ),
        include_invoke_tool=True,
    )
    monkeypatch.setattr(
        runtime,
        "delegation_tool_policy_error",
        lambda *, session, tool_name: "parent child policy denied capture" if tool_name == "capture" else None,
    )

    response = runtime.run(
        RuntimeRequest(
            prompt="dispatch from child",
            session_id="inner-policy",
        )
    )
    assert response.session.status == "completed"
    assert tool.calls == []
    denied = [event for event in response.events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "capture"]
    assert denied
    assert denied[-1].payload["status"] == "error"
    diagnostics = denied[-1].payload["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["kind"] == "delegation_policy_denied"
    assert not any(event.event_type == "runtime.tool_started" and event.payload.get("tool") == "capture" for event in response.events)

    assert denied[-1].payload["error"] == "parent child policy denied capture"


def test_rewrite_args_are_persisted_in_approval_and_resume_does_not_rewrite(tmp_path: Path) -> None:
    tool = _CaptureTool()
    handler_calls: list[str] = []
    registry = ToolInputHandlerRegistry((ToolInputHandlerBinding("canonicalize", _canonicalizer("approved.txt", handler_calls)),))
    runtime = _runtime(tmp_path, tool, registry, approval_mode="ask")
    waiting = runtime.run(RuntimeRequest(prompt="capture", session_id="approval-session"))
    approval = next(event for event in waiting.events if event.event_type == "runtime.approval_requested")
    assert approval.payload["arguments"] == {"path": "approved.txt"}
    resumed = runtime.resume("approval-session", approval_request_id=str(approval.payload["request_id"]), approval_decision="allow")
    assert resumed.session.status == "completed"
    assert handler_calls == ["capture"]
    assert [call.arguments for call in tool.calls] == [{"path": "approved.txt"}]


def test_invoke_tool_outer_is_not_rewritten_and_inner_runs_once(tmp_path: Path) -> None:
    tool = _CaptureTool()
    handler_calls: list[str] = []
    registry = ToolInputHandlerRegistry((ToolInputHandlerBinding("canonicalize", _canonicalizer("inner-final.txt", handler_calls)),))
    runtime = _runtime(
        tmp_path,
        tool,
        registry,
        initial_call=ToolCall(tool_name="invoke_tool", arguments={"name": "capture", "arguments": {"path": "./inner.txt"}}),
        include_invoke_tool=True,
    )
    response = runtime.run(RuntimeRequest(prompt="dispatch"))
    assert response.session.status == "completed"
    assert handler_calls == ["capture"]
    assert [call.arguments for call in tool.calls] == [{"path": "inner-final.txt"}]
    inner_request = next(
        event for event in response.events if event.event_type == "graph.tool_request_created" and event.payload.get("tool") == "capture"
    )
    assert inner_request.payload["arguments"] == {"path": "./inner.txt"}
    lookup = next(
        event for event in response.events if event.event_type == "runtime.tool_lookup_succeeded" and event.payload.get("tool") == "capture"
    )
    trace = next(event for event in response.events if event.event_type == "runtime.tool_input_processed")
    permission = next(event for event in response.events if event.event_type in {"runtime.permission_resolved", "runtime.approval_resolved"})
    assert inner_request.sequence < lookup.sequence < trace.sequence < permission.sequence
    metadata = trace.payload["rewrite"]
    assert isinstance(metadata, dict)
    assert isinstance(metadata["handler_names"], list)


def test_invoke_inner_block_emits_lookup_then_typed_trace_before_feedback(tmp_path: Path) -> None:
    tool = _CaptureTool()
    handler_calls: list[str] = []
    registry = ToolInputHandlerRegistry((ToolInputHandlerBinding("block", _blocker("blocked by typed policy", handler_calls)),))
    runtime = _runtime(
        tmp_path,
        tool,
        registry,
        initial_call=ToolCall(
            tool_name="invoke_tool",
            arguments={"name": "capture", "arguments": {"path": "./inner.txt"}},
        ),
        include_invoke_tool=True,
    )
    response = runtime.run(RuntimeRequest(prompt="dispatch blocked"))

    assert response.session.status == "completed"
    assert handler_calls == ["capture"]
    assert tool.calls == []
    inner_request = next(
        event for event in response.events if event.event_type == "graph.tool_request_created" and event.payload.get("tool") == "capture"
    )
    lookup = next(
        event for event in response.events if event.event_type == "runtime.tool_lookup_succeeded" and event.payload.get("tool") == "capture"
    )
    trace = next(
        event for event in response.events if event.event_type == "runtime.tool_input_processed" and event.payload.get("tool_name") == "capture"
    )
    feedback = next(event for event in response.events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "capture")
    assert inner_request.sequence < lookup.sequence < trace.sequence < feedback.sequence
    assert trace.payload["hook_status"] == "blocked"
    assert feedback.payload["status"] == "error"
    assert feedback.payload["error"] == "blocked by typed policy"


def test_invoke_inner_error_feedback_uses_final_rewritten_args(tmp_path: Path) -> None:
    tool = _CaptureTool(failure=ValueError("inner failed"))
    registry = ToolInputHandlerRegistry((ToolInputHandlerBinding("canonicalize", _canonicalizer("error-final.txt")),))
    runtime = _runtime(
        tmp_path,
        tool,
        registry,
        initial_call=ToolCall(
            tool_name="invoke_tool",
            arguments={"name": "capture", "arguments": {"path": "./inner.txt"}},
        ),
        include_invoke_tool=True,
    )
    response = runtime.run(RuntimeRequest(prompt="dispatch error"))
    completed = [event for event in response.events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "capture"]
    assert completed and completed[-1].payload["arguments"] == {"path": "error-final.txt"}


def test_invoke_inner_timeout_feedback_uses_final_rewritten_args(tmp_path: Path) -> None:
    tool = _CaptureTool(failure=RuntimeToolTimeoutError("timed out"))
    registry = ToolInputHandlerRegistry((ToolInputHandlerBinding("canonicalize", _canonicalizer("timeout-final.txt")),))
    runtime = _runtime(
        tmp_path,
        tool,
        registry,
        initial_call=ToolCall(
            tool_name="invoke_tool",
            arguments={"name": "capture", "arguments": {"path": "./inner.txt"}},
        ),
        include_invoke_tool=True,
    )
    response = runtime.run(RuntimeRequest(prompt="dispatch timeout"))
    completed = [event for event in response.events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "capture"]
    assert completed and completed[-1].payload["arguments"] == {"path": "timeout-final.txt"}
