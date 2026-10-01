"""Regression: a tool's runtime-owned write runs under the invoking execution's lease.

`RuntimeToolExecutor._invoke_with_progress` runs bounded/timeout tools on its own
worker thread. Runtime-owned commits made from inside such a tool — the audited
escape was the built-in ``background_process op=start`` registering a
``background_processes`` row after its execution had been seized — must be
attributed to the execution that invoked the tool. If the tool worker does not
inherit the caller's lease, a revoked execution keeps mutating runtime truth
through its tools.

The tests below pin that seam: the tool worker performs the same kind of commit
(a durable session event through the real `SqliteSessionStore`) and the test
holds it in flight while ownership is revoked.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.execution_ownership import EXECUTION_OWNERSHIP, ExecutionOwnershipRevokedError
from voidcode.runtime.storage.sqlite import SqliteSessionStore
from voidcode.runtime.tool_execution import RuntimeToolExecutor
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolInvocation, ToolResult

TASK_ID = "task-tool-thread-ownership"
#: Distinct task id for the control case: the refusal registry is process-global
#: and keyed by task id, so each case must own its key.
OWNED_TASK_ID = "task-tool-thread-ownership-live"
SESSION_ID = "session-tool-thread"
MARKER_EVENT = "runtime.tool_thread_marker"


class _GatedRuntimeWriteTool:
    """A tool that commits runtime truth from the executor's worker thread.

    Named ``shell_exec`` so the executor always takes its threaded path, and
    gated so the test can revoke ownership while the tool is in flight.
    """

    definition = ToolDefinition(name="shell_exec", description="writes runtime truth after a gate")

    def __init__(self, *, store: SqliteSessionStore, workspace: Path) -> None:
        self._store = store
        self._workspace = workspace
        self.entered = threading.Event()
        self.release = threading.Event()
        self.error: BaseException | None = None

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = context
        self.entered.set()
        assert self.release.wait(timeout=15.0), "the tool was never released"
        try:
            self._store.append_session_event(
                workspace=self._workspace,
                session_id=SESSION_ID,
                event_type=MARKER_EVENT,
                source="tool",
                payload={"tool": call.tool_name},
            )
        except BaseException as exc:  # noqa: BLE001 - the test asserts on the exact refusal
            self.error = exc
            raise
        return ToolResult(tool_name=call.tool_name, status="ok", content="wrote runtime truth")


def _store(tmp_path: Path, monkeypatch: Any) -> SqliteSessionStore:
    database_path = tmp_path / "state" / "voidcode" / "sessions.sqlite3"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(database_path))
    store = SqliteSessionStore()
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=SESSION_ID,
        prompt="tool-thread ownership",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    return store


def _marker_event_count(store: SqliteSessionStore, workspace: Path) -> int:
    stored = store.load_session(workspace=workspace, session_id=SESSION_ID)
    return sum(1 for event in stored.events if event.event_type == MARKER_EVENT)


def _drive_tool_thread(
    *,
    tmp_path: Path,
    monkeypatch: Any,
    task_id: str,
    revoke_while_in_flight: bool,
) -> tuple[_GatedRuntimeWriteTool, SqliteSessionStore, list[Any]]:
    """Run one gated tool invocation from a lease-bound execution thread."""
    store = _store(tmp_path, monkeypatch)
    lease = EXECUTION_OWNERSHIP.grant(workspace=tmp_path, task_id=task_id)
    tool = _GatedRuntimeWriteTool(store=store, workspace=tmp_path)
    executor = RuntimeToolExecutor(workspace=tmp_path)
    invocation = ToolInvocation(
        tool_call=ToolCall(tool_name="shell_exec", arguments={}, tool_call_id="call-1"),
        tool_definition=tool.definition,
        context=ToolContext(session_id=SESSION_ID, run_id="run-1", invocation_id="call-1"),
    )
    outcomes: list[Any] = []

    def consume() -> None:
        # The execution thread owns the lease; the executor's tool worker must
        # inherit it — that inheritance is the seam under test.
        with EXECUTION_OWNERSHIP.bind(lease):
            execution = executor.invoke(tool=tool, invocation=invocation)
            while True:
                try:
                    next(execution)
                except StopIteration as completed:
                    outcomes.append(completed.value)
                    return

    execution_thread = threading.Thread(target=consume, name="test-execution-thread")
    execution_thread.start()
    assert tool.entered.wait(timeout=10.0), "the tool never started on the executor worker thread"
    if revoke_while_in_flight:
        _ = EXECUTION_OWNERSHIP.revoke(
            workspace=tmp_path,
            task_id=task_id,
            reason="runtime shutdown deadline expired while the execution was in flight",
        )
    tool.release.set()
    execution_thread.join(timeout=20.0)
    assert not execution_thread.is_alive(), "the execution thread did not finish"
    return tool, store, outcomes


def test_revoked_execution_cannot_commit_truth_from_a_tool_thread(tmp_path: Path, monkeypatch: Any) -> None:
    """A tool's runtime-owned write is refused once its execution lost ownership."""
    tool, store, outcomes = _drive_tool_thread(tmp_path=tmp_path, monkeypatch=monkeypatch, task_id=TASK_ID, revoke_while_in_flight=True)

    assert isinstance(tool.error, ExecutionOwnershipRevokedError), f"the tool thread committed anyway: {tool.error!r}"
    assert _marker_event_count(store, tmp_path) == 0, "the revoked execution's tool wrote a durable row"
    diagnostics = [diagnostic for diagnostic in EXECUTION_OWNERSHIP.late_writes() if diagnostic.task_id == TASK_ID]
    assert diagnostics, "the refused tool-thread write was not recorded"
    assert diagnostics[0].operation == "session_store_write"
    assert diagnostics[0].thread.startswith("runtime-tool-"), f"refusal not attributed to the tool thread: {diagnostics[0]}"
    assert len(outcomes) == 1, "the execution did not observe exactly one terminal outcome"


def test_tool_thread_commits_are_authorized_while_the_execution_owns_the_task(tmp_path: Path, monkeypatch: Any) -> None:
    """Control: the same tool commit lands while the execution still owns the task."""
    tool, store, outcomes = _drive_tool_thread(tmp_path=tmp_path, monkeypatch=monkeypatch, task_id=OWNED_TASK_ID, revoke_while_in_flight=False)

    assert tool.error is None, f"an owned execution's tool was refused: {tool.error!r}"
    assert _marker_event_count(store, tmp_path) == 1, "the owned execution's tool write was lost"
    assert not [diagnostic for diagnostic in EXECUTION_OWNERSHIP.late_writes() if diagnostic.task_id == OWNED_TASK_ID]
    assert len(outcomes) == 1, "the execution did not observe exactly one terminal outcome"
