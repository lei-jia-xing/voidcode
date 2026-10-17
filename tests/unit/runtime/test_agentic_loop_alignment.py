from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from voidcode.core.tool_context import ToolContext
from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnRequest
from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY
from voidcode.runtime.config.loading import (
    ExternalDirectoryPermissionConfig,
    RuntimeConfig,
)
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permissions.policy import (
    PatternPermissionRule,
    PermissionPolicy,
)
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.storage.sqlite import SqliteSessionStore
from voidcode.tools.contracts import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolSuccess,
)


def _runtime_with_store(
    tmp_path: Path,
    store: SqliteSessionStore,
    *,
    tool_registry: ToolRegistry | None = None,
    config: RuntimeConfig | None = None,
    turn_producer: Any = None,
) -> VoidCodeRuntime:
    effective_config = config or RuntimeConfig(approval_mode="yolo")
    return VoidCodeRuntime(
        workspace=tmp_path,
        turn_producer=turn_producer,
        tool_registry=tool_registry,
        config=effective_config,
        permission_policy=PermissionPolicy(mode=effective_config.approval_mode),
    )


# ===========================================================================
# 1. STREAM-TIME CALL PERSISTENCE (Codex parity)
# ===========================================================================


def test_stream_time_call_persistence_survives_crash_and_resumes_without_duplicates(tmp_path: Path) -> None:
    """Intents for every call in a batch are persisted at batch observation time.

    When killed mid-batch after call 0 completes, all 3 intents are already durable
    in storage. Resuming completes call 1 and call 2 from durable prefix without
    re-executing call 0 and with zero duplicate request events.
    """
    store = SqliteSessionStore()
    executed_calls: list[str] = []

    class _TrackingTool:
        def __init__(self, name: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description=f"Tracking tool {name}",
                effects=frozenset({ToolEffect.READ}),
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            executed_calls.append(call.tool_name)
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput(f"result of {call.tool_name}"))

    tool0 = _TrackingTool("tool_0")
    tool1 = _TrackingTool("tool_1")
    tool2 = _TrackingTool("tool_2")
    registry = ToolRegistry.from_tools((tool0, tool1, tool2))

    calls = (
        ToolCall(tool_name="tool_0", arguments={"k": "v0"}, tool_call_id="call-0"),
        ToolCall(tool_name="tool_1", arguments={"k": "v1"}, tool_call_id="call-1"),
        ToolCall(tool_name="tool_2", arguments={"k": "v2"}, tool_call_id="call-2"),
    )

    class _CrashAfterFirstCallProducer:
        def produce(self, request: TurnRequest, tool_results: tuple[Any, ...], *, session: Any) -> ToolTurn | FinalTurn:
            fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
            if not tool_results:
                return ToolTurn(calls=calls, facts=(fact,))
            return FinalTurn(output="done", facts=(fact,))

    runtime1 = _runtime_with_store(tmp_path, store, tool_registry=registry, turn_producer=_CrashAfterFirstCallProducer())

    # Step the engine until call 0 completes, then simulate process crash by abandoning stream and clearing in-process registry
    stream = runtime1.run_stream(RuntimeRequest(prompt="start batch", session_id="crash-session"))
    for chunk in stream:
        if chunk.event is not None and chunk.event.event_type == "runtime.tool_completed":
            break
    stream.close()
    ACTIVE_SESSION_REGISTRY._runs.clear()

    assert executed_calls == ["tool_0"]

    # At crash time, inspect SQLite storage:
    stored = store.load_session(workspace=tmp_path, session_id="crash-session")

    # All three tool requests MUST be persisted durably at stream/observation time:
    request_events = [e for e in stored.events if e.event_type == "core.tool_request_created"]
    assert len(request_events) == 3
    assert [e.payload.get("tool_call_id") for e in request_events] == ["call-0", "call-1", "call-2"]

    # Only call-0 has completed:
    completed_events = [e for e in stored.events if e.event_type == "runtime.tool_completed"]
    assert len(completed_events) == 1
    assert completed_events[0].payload.get("reported_call", {}).get("tool_call_id") == "call-0"

    # Now resume in a fresh runtime instance
    runtime2 = _runtime_with_store(tmp_path, store, tool_registry=registry, turn_producer=_CrashAfterFirstCallProducer())
    resumed = runtime2.resume("crash-session")
    assert resumed.session.status == "completed"

    # call-0 was NOT re-executed; call-1 and call-2 executed on resume
    assert executed_calls == ["tool_0", "tool_1", "tool_2"]

    # Verify no duplicate core.tool_request_created events on session path
    path = store.session_path(workspace=tmp_path, session_id="crash-session")
    final_requests = [e for e in path if e.event_type == "core.tool_request_created"]
    assert len(final_requests) == 3
    assert [e.payload.get("tool_call_id") for e in final_requests] == ["call-0", "call-1", "call-2"]

    final_completions = [e for e in path if e.event_type == "runtime.tool_completed"]
    assert len(final_completions) == 3
    assert [e.payload.get("reported_call", {}).get("tool_call_id") for e in final_completions] == ["call-0", "call-1", "call-2"]


# ===========================================================================
# 2. CANCEL BACKFILL (Codex parity: serial + fan-out)
# ===========================================================================


def test_cancel_backfill_serial_persists_placeholders_and_resume_skips_reexecution(tmp_path: Path) -> None:
    """Cancelling mid-batch serially backfills aborted placeholders for all uncompleted calls.

    Resuming does NOT re-execute them.
    """
    store = SqliteSessionStore()
    executed: list[str] = []
    tool0_started = threading.Event()
    tool0_release = threading.Event()

    class _SerialBlockTool:
        def __init__(self, name: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description=f"Serial tool {name}",
                effects=frozenset({ToolEffect.WRITE}),
                supports_parallel=False,
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            executed.append(call.tool_name)
            tool0_started.set()
            tool0_release.wait(timeout=5.0)
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("done"))

    tool0 = _SerialBlockTool("s_tool_0")
    tool1 = _SerialBlockTool("s_tool_1")
    registry = ToolRegistry.from_tools((tool0, tool1))

    calls = (
        ToolCall(tool_name="s_tool_0", arguments={}, tool_call_id="call-s0"),
        ToolCall(tool_name="s_tool_1", arguments={}, tool_call_id="call-s1"),
    )

    class _Producer:
        def produce(self, request: TurnRequest, tool_results: tuple[Any, ...], *, session: Any) -> ToolTurn | FinalTurn:
            fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
            if not tool_results:
                return ToolTurn(calls=calls, facts=(fact,))
            return FinalTurn(output=f"done with {len(tool_results)} results", facts=(fact,))

    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry, turn_producer=_Producer())
    req = RuntimeRequest(prompt="serial cancel test", session_id="cancel-serial")

    def _drive() -> None:
        try:
            for _ in runtime.run_stream(req):
                pass
        except Exception:
            pass

    t = threading.Thread(target=_drive)
    t.start()

    assert tool0_started.wait(timeout=5.0)
    # Cancel while tool 0 is in flight
    cancel_res = runtime.cancel_session("cancel-serial", reason="cancel requested mid-flight")
    assert cancel_res.interrupted is True
    tool0_release.set()
    t.join(timeout=5.0)

    # Both s_tool_0 and s_tool_1 must have persisted terminal outcomes (aborted placeholders)
    stored = store.load_session(workspace=tmp_path, session_id="cancel-serial")
    completed_events = [e for e in stored.events if e.event_type == "runtime.tool_completed"]
    assert len(completed_events) == 2
    for event in completed_events:
        assert event.payload["status"] == "error"
        assert event.payload["cancellation_signalled"] is True
        assert event.payload["execution_stopped"] is True

    # Resuming the session should NOT re-execute s_tool_0 or s_tool_1
    ACTIVE_SESSION_REGISTRY._runs.clear()
    resumed = runtime.resume("cancel-serial")
    assert resumed.session.status in ("completed", "interrupted")
    # executed list still only has s_tool_0 from the initial start; s_tool_1 was never invoked
    assert executed == ["s_tool_0"]


def test_cancel_backfill_fanout_persists_placeholders_and_resume_skips_reexecution(tmp_path: Path) -> None:
    """Cancelling mid-fan-out backfills aborted placeholders in batch order on main thread.

    Resuming does NOT re-execute them.
    """
    store = SqliteSessionStore()
    executed: list[str] = []
    both_inside = threading.Barrier(3, timeout=5.0)  # 2 tools + test thread
    release_event = threading.Event()

    class _ParallelBlockTool:
        def __init__(self, name: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description=f"Parallel tool {name}",
                effects=frozenset({ToolEffect.READ}),
                supports_parallel=True,
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            executed.append(call.tool_name)
            both_inside.wait()
            release_event.wait(timeout=5.0)
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("done"))

    tool0 = _ParallelBlockTool("p_tool_0")
    tool1 = _ParallelBlockTool("p_tool_1")
    registry = ToolRegistry.from_tools((tool0, tool1))

    calls = (
        ToolCall(tool_name="p_tool_0", arguments={}, tool_call_id="call-p0"),
        ToolCall(tool_name="p_tool_1", arguments={}, tool_call_id="call-p1"),
    )

    class _Producer:
        def produce(self, request: TurnRequest, tool_results: tuple[Any, ...], *, session: Any) -> ToolTurn | FinalTurn:
            fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
            if not tool_results:
                return ToolTurn(calls=calls, facts=(fact,))
            return FinalTurn(output=f"done with {len(tool_results)} results", facts=(fact,))

    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry, turn_producer=_Producer())
    req = RuntimeRequest(prompt="fanout cancel test", session_id="cancel-fanout")

    def _drive() -> None:
        try:
            for _ in runtime.run_stream(req):
                pass
        except Exception:
            pass

    t = threading.Thread(target=_drive)
    t.start()

    # Wait until both tools are inside invoke concurrently
    both_inside.wait()

    # Cancel while mid-fanout
    cancel_res = runtime.cancel_session("cancel-fanout", reason="cancel fanout")
    assert cancel_res.interrupted is True
    release_event.set()
    t.join(timeout=5.0)

    # Both calls must have persisted terminal outcomes in batch order
    stored = store.load_session(workspace=tmp_path, session_id="cancel-fanout")
    completed_events = [e for e in stored.events if e.event_type == "runtime.tool_completed"]
    assert len(completed_events) == 2
    assert [e.payload.get("reported_call", {}).get("tool_call_id") for e in completed_events] == ["call-p0", "call-p1"]
    for event in completed_events:
        assert event.payload["status"] == "error"
        assert event.payload["cancellation_signalled"] is True

    # Resuming should NOT re-execute p_tool_0 or p_tool_1
    executed_before_resume = list(executed)
    ACTIVE_SESSION_REGISTRY._runs.clear()
    resumed = runtime.resume("cancel-fanout")
    assert resumed.session.status in ("completed", "interrupted")
    assert executed == executed_before_resume


# ===========================================================================
# 3. DENY AS MODEL-VISIBLE OUTPUT (Codex/Claude parity)
# ===========================================================================


def test_deny_continues_policy_rule_denial_runs_siblings_and_model_sees_denial(tmp_path: Path) -> None:
    """When a tool call in a batch is denied by policy rule, siblings still run

    and the model turn sees the denial text.
    """
    store = SqliteSessionStore()
    executed: list[str] = []
    seen_by_model: list[Any] = []

    class _ActionTool:
        def __init__(self, name: str, op_class: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description=f"Action tool {name}",
                effects=frozenset({ToolEffect.WRITE if op_class == "write" else ToolEffect.READ}),
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            executed.append(call.tool_name)
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput(f"output of {call.tool_name}"))

    tool_denied = _ActionTool("tool_denied", "write")
    tool_allowed = _ActionTool("tool_allowed", "read")
    registry = ToolRegistry.from_tools((tool_denied, tool_allowed))

    # Configure rule denying tool_denied explicitly
    config = RuntimeConfig(
        approval_mode="ask",
        permission=ExternalDirectoryPermissionConfig(rules=(PatternPermissionRule(tool="tool_denied", decision="deny"),)),
    )

    calls = (
        ToolCall(tool_name="tool_denied", arguments={"arg": "x"}, tool_call_id="call-deny"),
        ToolCall(tool_name="tool_allowed", arguments={"arg": "y"}, tool_call_id="call-allow"),
    )

    class _Producer:
        def produce(self, request: TurnRequest, tool_results: tuple[Any, ...], *, session: Any) -> ToolTurn | FinalTurn:
            fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
            if not tool_results:
                return ToolTurn(calls=calls, facts=(fact,))
            seen_by_model.extend(tool_results)
            return FinalTurn(output="handled denial", facts=(fact,))

    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry, config=config, turn_producer=_Producer())

    response = runtime.run(RuntimeRequest(prompt="test policy deny", session_id="policy-deny-session"))
    assert response.session.status == "completed"
    assert response.output == "handled denial"

    # Sibling tool_allowed MUST have executed:
    assert executed == ["tool_allowed"]

    # Model MUST have received both results: the denial and the sibling success:
    assert len(seen_by_model) == 2
    denial_result = next(r for r in seen_by_model if r.tool_name == "tool_denied")
    assert denial_result.status == "error"
    assert "permission denied" in str(denial_result.error).lower()

    allowed_result = next(r for r in seen_by_model if r.tool_name == "tool_allowed")
    assert allowed_result.status == "ok"


def test_deny_continues_approval_denial_runs_siblings_and_model_sees_denial(tmp_path: Path) -> None:
    """When an approval is denied by the user, siblings still run and model receives denial text."""
    store = SqliteSessionStore()
    executed: list[str] = []
    seen_by_model: list[Any] = []

    class _AskTool:
        def __init__(self, name: str, op_class: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description=f"Tool {name}",
                effects=frozenset({ToolEffect.WRITE if op_class == "write" else ToolEffect.READ}),
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            executed.append(call.tool_name)
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput(f"output of {call.tool_name}"))

    tool_ask = _AskTool("tool_ask", "write")
    tool_ok = _AskTool("tool_ok", "read")
    registry = ToolRegistry.from_tools((tool_ask, tool_ok))

    config = RuntimeConfig(approval_mode="ask")

    calls = (
        ToolCall(tool_name="tool_ask", arguments={"path": "x.txt"}, tool_call_id="call-ask"),
        ToolCall(tool_name="tool_ok", arguments={"path": "y.txt"}, tool_call_id="call-ok"),
    )

    class _Producer:
        def produce(self, request: TurnRequest, tool_results: tuple[Any, ...], *, session: Any) -> ToolTurn | FinalTurn:
            fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
            if not tool_results:
                return ToolTurn(calls=calls, facts=(fact,))
            seen_by_model.extend(tool_results)
            return FinalTurn(output="handled approval denial", facts=(fact,))

    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry, config=config, turn_producer=_Producer())

    # Step 1: run halts at tool_ask with status="waiting"
    res1 = runtime.run(RuntimeRequest(prompt="test approval deny", session_id="app-deny-session"))
    assert res1.session.status == "waiting"

    approval_request_id = next(e.payload["request_id"] for e in res1.events if e.event_type == "runtime.approval_requested")

    # Step 2: user denies approval
    res2 = runtime.resume(
        "app-deny-session",
        approval_request_id=approval_request_id,
        approval_decision="deny",
    )
    assert res2.session.status == "completed"
    assert res2.output == "handled approval denial"

    # Sibling tool_ok ran:
    assert executed == ["tool_ok"]

    # Model received both outcomes:
    assert len(seen_by_model) == 2
    denial_result = next(r for r in seen_by_model if r.tool_name == "tool_ask")
    assert denial_result.status == "error"
    assert "permission denied" in str(denial_result.error).lower()

    allowed_result = next(r for r in seen_by_model if r.tool_name == "tool_ok")
    assert allowed_result.status == "ok"
