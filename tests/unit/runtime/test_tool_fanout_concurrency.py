from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from tests.runtime_storage import repositories_for_test_store
from voidcode.core.tool_context import ToolContext
from voidcode.core.turns import CallSeed, FinalTurn, ModelTurnFact, ReportedCall, ToolTurn, TurnRequest
from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.permissions.policy import (
    ExternalDirectoryPermissionConfig,
)
from voidcode.runtime.service import RuntimeRequest, RuntimeStreamChunk, SessionState, ToolRegistry, VoidCodeRuntime
from voidcode.tools.contracts import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)


def _runtime_with_store(
    tmp_path: Path,
    store: Any,
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


def _turn_request(runtime: VoidCodeRuntime, *, session_id: str) -> tuple[SessionState, TurnRequest, ToolRegistry]:
    run_id = f"test-run-{uuid4().hex}"
    runtime_request = RuntimeRequest(prompt="probe", session_id=session_id)
    prepared = runtime._prepare_stream_request_and_rehydration(
        runtime_request,
        session_id=session_id,
        run_id=run_id,
    )
    session = runtime._assemble_stream_session_metadata(prepared, run_id=run_id)
    tool_registry = runtime.tool_registry_for_config(prepared.config)
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
        metadata=session.metadata,
        run_id=run_id,
    )
    return session, request, tool_registry


class _BatchProducer:
    def __init__(self, calls: tuple[ToolCall, ...]) -> None:
        self.calls = calls
        self.turn = 0

    def produce(self, request: TurnRequest, tool_results: tuple[Any, ...], *, session: SessionState) -> ToolTurn | FinalTurn:
        _ = tool_results, session
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        if self.turn == 0:
            self.turn += 1
            return ToolTurn(calls=self.calls, facts=(fact,))
        return FinalTurn(output="done", facts=(fact,))


class _AbortSignal:
    def __init__(self) -> None:
        self._cancelled = False
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        if value:
            self.reason = reason


class _MockBarrierReadTool:
    def __init__(self, name: str, barrier: threading.Barrier, inside_tracker: list[int], counter: list[int], lock: threading.Lock) -> None:
        self.definition = ToolDefinition(
            name=name,
            description="Mock parallel read tool.",
            effects=frozenset({ToolEffect.READ}),
            supports_parallel=True,
        )
        self.barrier = barrier
        self.inside_tracker = inside_tracker
        self.counter = counter
        self.lock = lock

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        with self.lock:
            self.counter[0] += 1
            self.inside_tracker.append(self.counter[0])
        try:
            self.barrier.wait()
        finally:
            with self.lock:
                self.counter[0] -= 1
        return ToolSuccess(
            tool_name=call.tool_name,
            output=TextOutput(f"read:{call.arguments.get('id', '')}"),
        )


def test_a_read_run_fanout_concurrent_execution_in_batch_order(tmp_path: Path) -> None:
    """(a) read-run fan-out: N flagged reads execute concurrently (all N inside at once), results in batch order."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store = SqliteSessionStore()

    n = 3
    barrier = threading.Barrier(n, timeout=5.0)
    tracker: list[int] = []
    counter = [0]
    lock = threading.Lock()

    tool1 = _MockBarrierReadTool("read_tool_1", barrier, tracker, counter, lock)
    tool2 = _MockBarrierReadTool("read_tool_2", barrier, tracker, counter, lock)
    tool3 = _MockBarrierReadTool("read_tool_3", barrier, tracker, counter, lock)

    registry = ToolRegistry.from_tools((tool1, tool2, tool3))
    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry)
    session, request, reg = _turn_request(runtime, session_id="s1")

    calls = (
        ToolCall(tool_name="read_tool_1", arguments={"id": "0"}, tool_call_id="call-0"),
        ToolCall(tool_name="read_tool_2", arguments={"id": "1"}, tool_call_id="call-1"),
        ToolCall(tool_name="read_tool_3", arguments={"id": "2"}, tool_call_id="call-2"),
    )
    producer = _BatchProducer(calls)
    tool_results: list[ReportedCall] = []

    _ = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=producer,
            tool_registry=reg,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=tool_results,
        )
    )

    # Prove all N were inside at once
    assert max(tracker) == n, f"Expected all {n} tools to be inside at once, got max {max(tracker)}"
    # Prove results are in original batch order
    assert len(tool_results) == n
    assert [r.final_tool_name for r in tool_results] == ["read_tool_1", "read_tool_2", "read_tool_3"]
    assert [r.final_arguments.get("id") for r in tool_results] == ["0", "1", "2"]


def test_b_unflagged_write_call_breaks_the_run(tmp_path: Path) -> None:
    """(b) unflagged/write call breaks the run (serial boundary)."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store = SqliteSessionStore()

    read_barrier = threading.Barrier(2, timeout=5.0)
    tracker: list[int] = []
    counter = [0]
    lock = threading.Lock()

    read1 = _MockBarrierReadTool("read_1", read_barrier, tracker, counter, lock)
    read2 = _MockBarrierReadTool("read_2", read_barrier, tracker, counter, lock)

    write_executed_after = threading.Event()

    class _MockWriteTool:
        definition = ToolDefinition(
            name="write_tool",
            description="Mock write tool with supports_parallel=False.",
            effects=frozenset({ToolEffect.WRITE}),
            supports_parallel=False,
        )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            # write should run ONLY after read1 and read2 have completely finished
            write_executed_after.set()
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("wrote"))

    registry = ToolRegistry.from_tools((read1, read2, _MockWriteTool()))
    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry)
    session, request, reg = _turn_request(runtime, session_id="s2")

    calls = (
        ToolCall(tool_name="read_1", arguments={"id": "0"}, tool_call_id="call-0"),
        ToolCall(tool_name="read_2", arguments={"id": "1"}, tool_call_id="call-1"),
        ToolCall(tool_name="write_tool", arguments={"path": "out.txt"}, tool_call_id="call-2"),
    )
    producer = _BatchProducer(calls)
    tool_results: list[ReportedCall] = []

    _ = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=producer,
            tool_registry=reg,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=tool_results,
        )
    )

    assert max(tracker) == 2
    assert write_executed_after.is_set()
    assert len(tool_results) == 3
    assert [r.final_tool_name for r in tool_results] == ["read_1", "read_2", "write_tool"]


def test_c_ask_needed_call_runs_whole_run_serially(tmp_path: Path) -> None:
    """(c) ask-needed call -> whole run serial (prove no thread use)."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store = SqliteSessionStore()

    invoked_threads: list[threading.Thread] = []

    class _TrackingReadTool:
        def __init__(self, name: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description="Tracking read tool.",
                effects=frozenset({ToolEffect.READ}),
                path_argument_keys=("path",),
                supports_parallel=True,
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            invoked_threads.append(threading.current_thread())
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("ok"))

    tool1 = _TrackingReadTool("tool_ok")
    tool2 = _TrackingReadTool("tool_ask")

    from voidcode.runtime.permissions.policy import ExternalDirectoryPolicy

    config = RuntimeConfig(permission=ExternalDirectoryPermissionConfig(read=ExternalDirectoryPolicy(rules=(("*", "ask"),))))

    registry = ToolRegistry.from_tools((tool1, tool2))
    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry, config=config)
    session, request, reg = _turn_request(runtime, session_id="s3")

    calls = (
        ToolCall(tool_name="tool_ok", arguments={"path": "a.txt"}, tool_call_id="call-0"),
        ToolCall(tool_name="tool_ask", arguments={"path": "/outside/b.txt"}, tool_call_id="call-1"),
    )
    producer = _BatchProducer(calls)
    tool_results: list[ReportedCall] = []

    _ = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=producer,
            tool_registry=reg,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=tool_results,
        )
    )

    # Prove NO thread pool worker was used for tool_ok: executed directly on main thread!
    assert len(invoked_threads) == 1
    assert invoked_threads[0] is threading.main_thread(), f"Expected main thread, got {invoked_threads[0]}"
    # tool_ask paused for approval, so tool_results has only tool_ok
    assert len(tool_results) == 1
    assert tool_results[0].final_tool_name == "tool_ok"


def test_d_first_failure_cancels_rest(tmp_path: Path) -> None:
    """(d) first-failure-cancels-rest."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store = SqliteSessionStore()

    call1_cancel_observed = threading.Event()
    call0_started = threading.Event()

    class _FailingTool:
        definition = ToolDefinition(
            name="fail_tool",
            description="Tool that fails.",
            effects=frozenset({ToolEffect.READ}),
            supports_parallel=True,
        )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            call0_started.set()
            raise RuntimeError("call_0 failed explicitly")

    class _CancelAwareTool:
        definition = ToolDefinition(
            name="slow_tool",
            description="Tool that waits and checks cancel.",
            effects=frozenset({ToolEffect.READ}),
            supports_parallel=True,
        )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            call0_started.wait(timeout=2.0)
            # Wait for cancellation signal to be asserted due to call_0 failure
            for _ in range(100):
                if context.abort_signal is not None and context.abort_signal.cancelled:
                    call1_cancel_observed.set()
                    return ToolFailure(tool_name=call.tool_name, error="cancelled")
                threading.Event().wait(0.02)
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("survived"))

    registry = ToolRegistry.from_tools((_FailingTool(), _CancelAwareTool()))
    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry)
    session, request, reg = _turn_request(runtime, session_id="s4")

    calls = (
        ToolCall(tool_name="fail_tool", arguments={}, tool_call_id="call-0"),
        ToolCall(tool_name="slow_tool", arguments={}, tool_call_id="call-1"),
    )
    producer = _BatchProducer(calls)
    tool_results: list[ReportedCall] = []

    _ = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=producer,
            tool_registry=reg,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=tool_results,
        )
    )

    assert call1_cancel_observed.is_set(), "Call 1 should have observed cancellation signal"
    assert len(tool_results) == 2
    assert tool_results[0].result.status == "error"
    assert "call_0 failed" in str(tool_results[0].result.error)
    assert tool_results[1].result.status == "error"


def test_e_interactive_tool_never_fanned_out_even_if_flagged(tmp_path: Path) -> None:
    """(e) interactive tool never fanned out even if flagged True in test (flag must not override exclusion)."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store = SqliteSessionStore()

    threads_used: list[threading.Thread] = []

    class _FlaggedQuestionTool:
        definition = ToolDefinition(
            name="question",
            description="Question tool maliciously flagged supports_parallel=True in test.",
            effects=frozenset({ToolEffect.READ}),
            supports_parallel=True,  # Flag must not override hard exclusion!
        )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            threads_used.append(threading.current_thread())
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("question answered"))

    class _FlaggedReadTool:
        definition = ToolDefinition(
            name="read_tool",
            description="Parallel read tool.",
            effects=frozenset({ToolEffect.READ}),
            supports_parallel=True,
        )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            threads_used.append(threading.current_thread())
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("read ok"))

    registry = ToolRegistry.from_tools((_FlaggedQuestionTool(), _FlaggedReadTool()))
    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry)
    session, request, reg = _turn_request(runtime, session_id="s5")

    calls = (
        ToolCall(tool_name="question", arguments={}, tool_call_id="call-0"),
        ToolCall(tool_name="read_tool", arguments={}, tool_call_id="call-1"),
    )
    producer = _BatchProducer(calls)
    tool_results: list[ReportedCall] = []

    _ = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=producer,
            tool_registry=reg,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=tool_results,
        )
    )

    # Prove question ran on main thread (serial), never fanned out
    assert threads_used[0] is threading.main_thread(), f"Expected main thread, got {threads_used[0]}"
    assert len(tool_results) == 2


def test_f_checkpoint_prefix_invariant_holds_after_concurrent_run(tmp_path: Path) -> None:
    """(f) checkpoint prefix invariant holds after concurrent run."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store = SqliteSessionStore()

    class _SimpleReadTool:
        def __init__(self, name: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description="Simple read tool.",
                effects=frozenset({ToolEffect.READ}),
                supports_parallel=True,
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput(f"content of {call.tool_name}"))

    tool0 = _SimpleReadTool("read_0")
    tool1 = _SimpleReadTool("read_1")

    registry = ToolRegistry.from_tools((tool0, tool1))
    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry)
    session, request, reg = _turn_request(runtime, session_id="s6")

    calls = (
        ToolCall(tool_name="read_0", arguments={}, tool_call_id="call-0"),
        ToolCall(tool_name="read_1", arguments={}, tool_call_id="call-1"),
    )
    producer = _BatchProducer(calls)
    tool_results: list[ReportedCall] = []

    _ = list(
        runtime._run_loop_coordinator.execute_turn_engine(
            producer=producer,
            tool_registry=reg,
            session=session,
            sequence=0,
            turn_request=request,
            tool_results=tool_results,
        )
    )

    assert len(tool_results) == 2

    # Now verify checkpoint prefix recovery: start a fresh TurnEngine seeded with the completed batch
    batch_seed = CallSeed(
        calls=calls,
        completed_reports=tuple(tool_results),
        tool_batch_id=tool_results[0].tool_batch_id,
        run_step=1,
    )

    class _MockHost:
        def prepare(self, state: Any) -> Any:
            return None
            yield

        def invoke(self, producer: Any, state: Any) -> Any:
            return None
            yield

        def observe(self, plan: Any, state: Any, batch: Any) -> Any:
            return None
            yield

        def execute(self, call: Any, state: Any) -> Any:
            return None
            yield

        def finish(self, plan: Any, state: Any) -> Any:
            return None
            yield

        def drain_messages(self, *, kind: str) -> tuple[str, ...]:
            return ()

    class _DoneProducer:
        def produce(self, req: TurnRequest, tool_results: tuple[Any, ...], *, session: Any) -> FinalTurn:
            fact = ModelTurnFact(req.run_step, "deterministic", req.prompt)
            return FinalTurn(output="recovered done", facts=(fact,))

    from voidcode.core.engine import TurnEngine

    engine_gen = TurnEngine(_DoneProducer()).run(request, host=_MockHost(), seed=batch_seed)
    assert engine_gen is not None


def test_g_cancel_mid_fanout_publishes_nothing_after_cancel(tmp_path: Path) -> None:
    """(g) cancel mid-fan-out publishes nothing after cancel."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store = SqliteSessionStore()

    entered_barrier = threading.Barrier(3, timeout=5.0)  # 2 tools + test thread
    release_event = threading.Event()

    class _HangingParallelTool:
        def __init__(self, name: str) -> None:
            self.definition = ToolDefinition(
                name=name,
                description="Hanging tool for abort test.",
                effects=frozenset({ToolEffect.READ}),
                supports_parallel=True,
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            entered_barrier.wait()
            release_event.wait(timeout=2.0)
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput("late result"))

    tool0 = _HangingParallelTool("hang_0")
    tool1 = _HangingParallelTool("hang_1")

    registry = ToolRegistry.from_tools((tool0, tool1))
    runtime = _runtime_with_store(tmp_path, store, tool_registry=registry)
    session, request, reg = _turn_request(runtime, session_id="s7")
    abort_sig = _AbortSignal()
    request = replace(request, abort_signal=abort_sig)
    calls = (
        ToolCall(tool_name="hang_0", arguments={}, tool_call_id="call-0"),
        ToolCall(tool_name="hang_1", arguments={}, tool_call_id="call-1"),
    )
    producer = _BatchProducer(calls)
    tool_results: list[ReportedCall] = []

    # Run execute_turn_engine in a generator
    gen = runtime._run_loop_coordinator.execute_turn_engine(
        producer=producer,
        tool_registry=reg,
        session=session,
        sequence=0,
        turn_request=request,
        tool_results=tool_results,
    )

    def _drive_until_blocked() -> list[RuntimeStreamChunk]:
        collected = []
        try:
            for item in gen:
                collected.append(item)
        except StopIteration:
            pass
        return collected

    drive_thread = threading.Thread(target=_drive_until_blocked)
    drive_thread.start()

    # Wait until both tools are inside invoke
    entered_barrier.wait()

    # Cancel while mid-fan-out
    abort_sig.set_cancelled(True, reason="cancelled mid fanout")
    # Release tools
    release_event.set()
    drive_thread.join(timeout=3.0)

    # After cancellation, nothing is published to tool_results
    assert len(tool_results) == 0, f"Expected 0 published tool results after cancel, got {len(tool_results)}"


def test_h_streaming_event_order_matches_serial_order(tmp_path: Path) -> None:
    """(h) streaming/event order matches serial order."""
    from voidcode.runtime.storage.sqlite import SqliteSessionStore

    store_serial = SqliteSessionStore()

    store_fanout = SqliteSessionStore()

    class _OrderedReadTool:
        def __init__(self, name: str, parallel: bool) -> None:
            self.definition = ToolDefinition(
                name=name,
                description="Ordered read tool.",
                effects=frozenset({ToolEffect.READ}),
                supports_parallel=parallel,
            )

        def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
            return ToolSuccess(tool_name=call.tool_name, output=TextOutput(f"read {call.arguments.get('v')}"))

    # Serial run: supports_parallel=False
    serial_reg = ToolRegistry.from_tools((_OrderedReadTool("r1", False), _OrderedReadTool("r2", False)))
    serial_rt = _runtime_with_store(tmp_path, store_serial, tool_registry=serial_reg)
    s_session, s_request, s_reg = _turn_request(serial_rt, session_id="serial-s")

    calls = (
        ToolCall(tool_name="r1", arguments={"v": "1"}, tool_call_id="call-1"),
        ToolCall(tool_name="r2", arguments={"v": "2"}, tool_call_id="call-2"),
    )

    serial_chunks = list(
        serial_rt._run_loop_coordinator.execute_turn_engine(
            producer=_BatchProducer(calls),
            tool_registry=s_reg,
            session=s_session,
            sequence=0,
            turn_request=s_request,
            tool_results=[],
        )
    )

    # Fanout run: supports_parallel=True
    fanout_reg = ToolRegistry.from_tools((_OrderedReadTool("r1", True), _OrderedReadTool("r2", True)))
    fanout_rt = _runtime_with_store(tmp_path, store_fanout, tool_registry=fanout_reg)
    f_session, f_request, f_reg = _turn_request(fanout_rt, session_id="fanout-s")

    fanout_chunks = list(
        fanout_rt._run_loop_coordinator.execute_turn_engine(
            producer=_BatchProducer(calls),
            tool_registry=f_reg,
            session=f_session,
            sequence=0,
            turn_request=f_request,
            tool_results=[],
        )
    )

    serial_events = [c.event.event_type for c in serial_chunks if c.event is not None]
    fanout_events = [c.event.event_type for c in fanout_chunks if c.event is not None]

    assert fanout_events == serial_events, f"Fanout events {fanout_events} did not match serial events {serial_events}"
