"""Local workflow tests demonstrating core + binding + task/context composition without VoidCodeRuntime."""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from pathlib import Path

import pytest

from voidcode.core.engine import EngineResult, TurnEngine
from voidcode.core.memory_host import MemoryAbortSignal, MemoryHost
from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import ToolResultView, output_text
from voidcode.core.turns import (
    FinalTurn,
    ToolCompletedFact,
    ToolRequestedFact,
    ToolTurn,
    TurnFact,
    TurnPlan,
    TurnRequest,
    TurnSession,
)
from voidcode.runtime.composition import (
    CapabilityBinding,
    CompositionOwner,
    CompositionRef,
    FrozenComposition,
    SessionCompositionOwner,
    TaskCompositionOwner,
)
from voidcode.tools.contracts import OpaqueToolBody, TextOutput, ToolCall, ToolResult, ToolSuccess
from voidcode.tools.delegation.task import TaskTool
from voidcode.tools.read import ReadTool


class StepTurnProducer:
    """Minimal deterministic producer driving turns step-by-step."""

    def __init__(self, steps: Sequence[Callable[[TurnRequest, tuple[ToolResultView, ...]], TurnPlan]]) -> None:
        self.steps = tuple(steps)

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> TurnPlan:
        _ = session
        step_idx = request.run_step - 1
        if step_idx < len(self.steps):
            return self.steps[step_idx](request, tool_results)
        return FinalTurn(output="default completed")


def _run_engine(engine: TurnEngine, request: TurnRequest, *, host: MemoryHost) -> tuple[EngineResult, list[TurnFact]]:
    facts: list[TurnFact] = []
    gen: Generator[TurnFact, None, EngineResult] = engine.run(request, host=host)
    while True:
        try:
            facts.append(next(gen))
        except StopIteration as stop:
            return stop.value, facts


def test_local_workflow_multi_turn_tool_execution(tmp_path: Path) -> None:
    """Proves pure TurnEngine + MemoryHost + explicit ToolContext drive multi-turn execution."""
    (tmp_path / "sample.txt").write_text("hello core engine\n", encoding="utf-8")
    read_tool = ReadTool()
    host = MemoryHost(
        tools=(read_tool,),
        tool_context=ToolContext(workspace=tmp_path, session_id="session-local-1"),
    )

    producer = StepTurnProducer(
        [
            lambda req, res: ToolTurn(calls=(ToolCall("read", {"path": "sample.txt"}, tool_call_id="call-read-1"),)),
            lambda req, res: FinalTurn(output=f"Read output: {(output_text(res[0].output) or '').strip()}"),
        ]
    )
    engine = TurnEngine(producer)
    result, facts = _run_engine(engine, host.request("read file"), host=host)

    assert result.status == "completed"
    assert result.output is not None
    assert "hello core engine" in result.output
    assert len(result.tool_results) == 1
    assert result.tool_results[0].final_tool_name == "read"

    requested_facts = [f for f in facts if isinstance(f, ToolRequestedFact)]
    completed_facts = [f for f in facts if isinstance(f, ToolCompletedFact)]
    assert len(requested_facts) == 1
    assert len(completed_facts) == 1
    assert completed_facts[0].report.result.status == "ok"


def test_local_workflow_hierarchical_task_delegation_without_runtime_service(tmp_path: Path) -> None:
    """Proves TaskTool delegation runs child engine and correlates sessions without VoidCodeRuntime."""
    (tmp_path / "subtask_data.json").write_text('{"count": 42}', encoding="utf-8")
    read_tool = ReadTool()
    task_tool = TaskTool()

    child_session_id_recorded: list[str] = []

    def local_delegation_handler(call: ToolCall, *, context: ToolContext) -> ToolResult:
        assert context.session_id is not None
        child_host = MemoryHost(
            tools=(read_tool,),
            tool_context=ToolContext(workspace=tmp_path, session_id="child-session-99"),
        )
        child_session_id_recorded.append(child_host.session.session_id)

        child_producer = StepTurnProducer(
            [
                lambda req, res: ToolTurn(calls=(ToolCall("read", {"path": "subtask_data.json"}, tool_call_id="c-call-1"),)),
                lambda req, res: FinalTurn(output=f"Child read: {(output_text(res[0].output) or '').strip()}"),
            ]
        )
        child_engine = TurnEngine(child_producer)
        child_result, child_facts = _run_engine(child_engine, child_host.request("process data"), host=child_host)

        assert child_result.status == "completed"
        return ToolSuccess(
            tool_name="task",
            output=TextOutput(text=str(child_result.output)),
            body=OpaqueToolBody(
                structured_content={
                    "task_id": "child-task-1",
                    "parent_session_id": context.session_id,
                    "child_session_id": child_host.session.session_id,
                    "child_facts_count": len(child_facts),
                    "status": "completed",
                }
            ),
        )

    parent_host = MemoryHost(
        tools=(task_tool,),
        tool_context=ToolContext(
            workspace=tmp_path,
            session_id="parent-session-1",
            task_runtime=local_delegation_handler,
        ),
    )

    parent_producer = StepTurnProducer(
        [
            lambda req, res: ToolTurn(
                calls=(
                    ToolCall(
                        "task",
                        {
                            "prompt": "Read subtask json",
                            "run_in_background": False,
                            "subagent_type": "worker",
                            "load_skills": [],
                        },
                        tool_call_id="p-call-1",
                    ),
                )
            ),
            lambda req, res: FinalTurn(output=f"Parent received: {output_text(res[0].output) or ''}"),
        ]
    )
    parent_engine = TurnEngine(parent_producer)
    parent_result, parent_facts = _run_engine(parent_engine, parent_host.request("delegate subtask"), host=parent_host)

    assert parent_result.status == "completed"
    assert parent_result.output is not None
    assert "Child read:" in parent_result.output
    assert '{"count": 42}' in parent_result.output
    assert len(child_session_id_recorded) == 1

    # Verify delegation facts recorded cleanly on parent host
    completed = next(f for f in parent_facts if isinstance(f, ToolCompletedFact))
    assert completed.report.final_tool_name == "task"
    assert completed.report.result.status == "ok"


def test_local_workflow_capability_binding_and_ownership_references(tmp_path: Path) -> None:
    """Proves CapabilityBinding, FrozenComposition and CompositionRef compose with session and task."""
    owner = CompositionOwner()
    frozen = owner.prepare((), intent={"workflow": "in_memory_fixture", "delegation": "enabled"})

    assert isinstance(frozen, FrozenComposition)
    assert isinstance(frozen.binding, CapabilityBinding)
    frozen.verify()

    session_ref = frozen.reference(
        workspace=str(tmp_path),
        owner=SessionCompositionOwner(kind="session", session_id="sess-alpha"),
    )
    task_ref = frozen.reference(
        workspace=str(tmp_path),
        owner=TaskCompositionOwner(kind="task", task_id="task-beta"),
    )

    assert isinstance(session_ref, CompositionRef)
    assert isinstance(task_ref, CompositionRef)
    assert session_ref.binding_id == frozen.binding.binding_id
    assert task_ref.binding_id == frozen.binding.binding_id
    assert session_ref.owner.kind == "session"
    assert task_ref.owner.kind == "task"


def test_local_workflow_explicit_context_guards() -> None:
    """Proves explicit ToolContext contracts prevent uncoordinated ambient execution."""
    task_tool = TaskTool()
    call = ToolCall(
        "task",
        {"prompt": "unbound", "run_in_background": False, "subagent_type": "worker", "load_skills": []},
        tool_call_id="call-unbound",
    )

    # Missing session_id must be rejected
    with pytest.raises(RuntimeError, match="explicit session identity"):
        task_tool.invoke(call, context=ToolContext(session_id=None))

    # Missing task_runtime must be rejected
    with pytest.raises(RuntimeError, match="runtime-owned task command"):
        task_tool.invoke(call, context=ToolContext(session_id="valid-session", task_runtime=None))


def test_local_workflow_abort_signal() -> None:
    """Proves MemoryAbortSignal aborts TurnEngine cleanly without background supervisor."""
    abort = MemoryAbortSignal()
    read_tool = ReadTool()
    host = MemoryHost(tools=(read_tool,), abort_signal=abort)

    producer = StepTurnProducer(
        [
            lambda req, res: ToolTurn(calls=(ToolCall("read", {"path": "missing.txt"}, tool_call_id="c1"),)),
        ]
    )
    engine = TurnEngine(producer)

    abort.set_cancelled(True, reason="user abort")
    result, _ = _run_engine(engine, host.request("aborted run"), host=host)

    assert result.status == "aborted"
