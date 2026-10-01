"""Late writes after a seizure: where execution ownership stops.

The invariant under test is the one written down in
``docs/contracts/execution-lifecycle.md`` → 「执行所有权与 late write」 and
``docs/contracts/background-task-delegation.md`` → 「执行所有权与 late write」:

* an execution the runtime seized (shutdown deadline expired while it was in
  flight) keeps losing its writes wherever they are made on its behalf — the run
  loop's own commits *and* the commits a tool makes while it runs for that
  execution, because the tool-executor worker thread inherits the caller's lease;
* the lease is thread-scoped, so a writer thread the *tool itself* spawns is
  outside the guarantee. That boundary is asserted here as the documented
  boundary, not as a guarantee.

The resume-after-seizure counterpart is covered by
``tests/integration/test_keep_alive_subagent.py``: the new execution commits
under a fresh generation while the seized worker stays refused
(``test_keep_alive_interrupted_resume_runs_under_a_new_execution_and_commits``,
``test_keep_alive_resume_does_not_reauthorize_the_seized_worker``).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.execution_ownership import EXECUTION_OWNERSHIP, ExecutionOwnershipRevokedError
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult

TOOL_TASK_ID = "task-tool-late-write"
BOUNDARY_TASK_ID = "task-boundary-late-write"
SESSION_ID = "late-write-session"
MARKER_EVENT = "runtime.late_write_marker"
WRITER_TOOL = "truth_writing_tool"
SEIZURE_REASON = "runtime shutdown deadline expired while the execution was in flight"
TOOL_TIMEOUT_SECONDS = 30


def _append_marker(store: SqliteSessionStore, workspace: Path, session_id: str, marker: str) -> None:
    """Commit one runtime-owned marker event through the storage write gateway."""
    store.append_session_events(
        workspace=workspace,
        session_id=session_id,
        events=((MARKER_EVENT, "runtime", {"marker": marker}, None),),
    )


def _markers(workspace: Path, session_id: str = SESSION_ID) -> list[object]:
    stored = SqliteSessionStore().load_session(workspace=workspace, session_id=session_id)
    return [event.payload.get("marker") for event in stored.events if event.event_type == MARKER_EVENT]


class _GraphStep:
    def __init__(self, *, tool_call: ToolCall | None = None, output: str | None = None, is_finished: bool = False) -> None:
        self.events: tuple[object, ...] = ()
        self.tool_call = tool_call
        self.output = output
        self.is_finished = is_finished
        self.reasoning: str | None = None
        self.provider_usage: object | None = None


class _SingleToolGraph:
    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    def step(self, request: object, tool_results: tuple[object, ...], *, session: object) -> _GraphStep:
        _ = request, session
        if not tool_results:
            return _GraphStep(tool_call=ToolCall(tool_name=self._tool_name, arguments={}))
        return _GraphStep(output="done", is_finished=True)


class _Gate:
    """Explicit handshake: the tool signals entry, the test revokes, then releases."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_for_entry(self) -> None:
        self.entered.set()
        assert self.release.wait(timeout=20.0), "the tool was never released"


class _TruthWritingTool:
    """Tool that commits runtime truth itself, after the test revoked the lease."""

    definition = ToolDefinition(
        name=WRITER_TOOL,
        description="Commits runtime truth from inside the tool.",
        effects=frozenset({ToolEffect.WRITE}),
    )

    def __init__(self, *, workspace: Path, store: SqliteSessionStore) -> None:
        self._workspace = workspace
        self._store = store
        self.gate = _Gate()
        self.outcomes: dict[str, str] = {}

    def _write(self, marker: str) -> str:
        try:
            _append_marker(self._store, self._workspace, SESSION_ID, marker)
        except ExecutionOwnershipRevokedError as exc:
            return f"refused:{exc.code}"
        return "landed"

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = call, context
        self.outcomes["thread"] = threading.current_thread().name
        self.gate.wait_for_entry()
        self.outcomes["tool_commit"] = self._write("tool_commit")
        return ToolResult(tool_name=self.definition.name, status="ok", content="done")


class _BoundaryTool(_TruthWritingTool):
    """Same as the truth writer, plus a writer thread the tool spawns itself."""

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = call, context
        self.outcomes["thread"] = threading.current_thread().name
        self.gate.wait_for_entry()
        self.outcomes["tool_commit"] = self._write("tool_commit")

        def _spawned_writer() -> None:
            self.outcomes["spawned_commit"] = self._write("spawned_commit")

        writer = threading.Thread(target=_spawned_writer, name="tool-spawned-writer")
        writer.start()
        writer.join(timeout=20.0)
        assert not writer.is_alive()
        return ToolResult(tool_name=self.definition.name, status="ok", content="done")


def _seize_execution(tmp_path: Path, tool: _TruthWritingTool, *, task_id: str) -> dict[str, Any]:
    """Run one execution whose lease is revoked while its tool is running.

    The run executes on its own thread under the granted lease, exactly like a
    dispatched background execution; the revocation is the one a shutdown
    deadline performs, and it happens strictly before the tool is released.
    """
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([tool]),
        graph=_SingleToolGraph(WRITER_TOOL),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            execution_engine="deterministic",
            tool_timeout_seconds=TOOL_TIMEOUT_SECONDS,
        ),
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    lease = EXECUTION_OWNERSHIP.grant(workspace=tmp_path, task_id=task_id)
    outcome: dict[str, Any] = {"events": []}

    def _run_execution() -> None:
        with EXECUTION_OWNERSHIP.bind(lease):
            try:
                for chunk in runtime.run_stream(RuntimeRequest(prompt="go", session_id=SESSION_ID)):
                    if chunk.event is not None:
                        outcome["events"].append(chunk.event.event_type)
            except BaseException as exc:  # noqa: BLE001 - the test asserts on the recorded error
                outcome["error"] = exc

    execution = threading.Thread(target=_run_execution, name="late-write-execution")
    execution.start()
    assert tool.gate.entered.wait(timeout=20.0), "the execution never reached the truth-writing tool"
    EXECUTION_OWNERSHIP.revoke(workspace=tmp_path, task_id=task_id, reason=SEIZURE_REASON)
    tool.gate.release.set()
    execution.join(timeout=20.0)
    assert not execution.is_alive()
    outcome["lease"] = lease
    return outcome


def _refusals(task_id: str) -> list[Any]:
    return [diagnostic for diagnostic in EXECUTION_OWNERSHIP.late_writes() if diagnostic.task_id == task_id]


def test_a_seized_execution_loses_the_commits_its_tool_makes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Revocation reaches the tool-executor thread: the tool's own commit is refused too.

    This is the boundary the release audit made real — a seized execution used to
    keep committing truth through its tools, because a tool with a runtime timeout
    runs on a worker thread the executor owns. That thread now inherits the
    caller's lease, so the tool's commit is refused and recorded as a late write.
    """
    store = SqliteSessionStore()
    tool = _TruthWritingTool(workspace=tmp_path, store=store)
    with caplog.at_level(logging.WARNING, logger="voidcode.runtime.execution_ownership"):
        outcome = _seize_execution(tmp_path, tool, task_id=TOOL_TASK_ID)
    lease = outcome["lease"]

    assert tool.outcomes["tool_commit"] == "refused:execution_ownership_revoked"
    assert tool.outcomes["thread"].startswith(f"runtime-tool-{WRITER_TOOL}-"), tool.outcomes["thread"]
    assert _markers(tmp_path) == []
    # The seized execution also lost the run loop's own commit of the tool result.
    assert isinstance(outcome.get("error"), ExecutionOwnershipRevokedError), outcome.get("error")
    assert "runtime.tool_started" in outcome["events"]
    assert "runtime.tool_completed" not in outcome["events"]

    refusals = _refusals(TOOL_TASK_ID)
    assert refusals, "the refused tool commit was not recorded as a late-write diagnostic"
    assert all(diagnostic.generation == lease.generation for diagnostic in refusals)
    assert all(diagnostic.reason == SEIZURE_REASON for diagnostic in refusals)
    # ``session_store_write`` covers both the tool's commit and the run loop's
    # commit of the tool result. Each refusal is attributed to the thread that
    # made it, so the recorded threads and counts agree with the warnings.
    assert sorted(diagnostic.thread for diagnostic in refusals) == [
        "late-write-execution",
        f"runtime-tool-{WRITER_TOOL}-worker",
    ]
    assert all(diagnostic.count == 1 for diagnostic in refusals)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"refusing thread=runtime-tool-{WRITER_TOOL}-worker" in logged
    assert "refusing thread=late-write-execution" in logged


def test_a_writer_thread_spawned_by_a_tool_is_outside_the_lease_boundary(tmp_path: Path) -> None:
    """The documented boundary: the lease is thread-scoped and does not follow spawned threads.

    ``docs/contracts/background-task-delegation.md`` → 「执行所有权与 late write」
    states the checkable invariant: runtime-owned commits must happen on the
    execution's own thread, so a manager that spawns its own persisting thread
    steps outside the guarantee. This test pins that boundary — the tool's own
    commit (executor thread) is refused, the writer thread the tool spawns itself
    is not — so the claim stays falsifiable. If a later change propagates the
    lease into tool-spawned threads, this test and the contract must move together.
    """
    store = SqliteSessionStore()
    tool = _BoundaryTool(workspace=tmp_path, store=store)
    outcome = _seize_execution(tmp_path, tool, task_id=BOUNDARY_TASK_ID)

    assert tool.outcomes["tool_commit"] == "refused:execution_ownership_revoked"
    assert tool.outcomes["spawned_commit"] == "landed"
    assert _markers(tmp_path) == ["spawned_commit"]
    assert isinstance(outcome.get("error"), ExecutionOwnershipRevokedError), outcome.get("error")
    refusals = _refusals(BOUNDARY_TASK_ID)
    assert refusals, "the tool's own commit on the executor thread was not refused"
    # Exactly two refused commits, one entry each, both made on threads of the
    # seized execution (the tool's own commit and the run loop's commit of its
    # result); the writer thread the tool spawned contributed none.
    assert sorted(diagnostic.thread for diagnostic in refusals) == [
        "late-write-execution",
        f"runtime-tool-{WRITER_TOOL}-worker",
    ]
    assert sum(diagnostic.count for diagnostic in refusals) == 2
