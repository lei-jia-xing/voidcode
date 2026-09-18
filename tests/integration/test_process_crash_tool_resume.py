"""Process-crash recovery for a tool call that was in flight.

The scenario is a real crash: the child process executes a run that is stopped
hard (``SIGKILL``) while a tool invocation is in flight, and a *different* runtime
instance in this process then resumes the session. What is asserted is the
persisted truth of the crashed run and what the resume does with it:

* the completed tool call is durable exactly once and is never re-executed;
* the in-flight tool call is recorded as a started call with a pending intent and
  no completion, and the resume never claims it completed;
* the orphaned tail (the crashed call's own events) is truncated by the resume,
  and the call the resumed run makes is a fresh invocation with a new id;
* a crash *before* any checkpoint exists leaves a session whose resume cannot be
  honoured (no recorded capability snapshot to replay) — then the resume must
  refuse without having rewritten the persisted truth.

The invariant text lives in ``docs/contracts/execution-lifecycle.md`` and
``docs/contracts/agent-tool-calling.md`` → 「取消与超时（execution lifecycle）」.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from voidcode.runtime.contracts import RuntimeRequestError
from voidcode.runtime.storage import SqliteSessionStore

_CRASH_CHILD_SOURCE = '''
"""Child side of the process-crash acceptance test (also imported by the test)."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.tools import ReadTool
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult

SESSION_ID = "crash-tool-session"
COUNTING_READ_TOOL = "counting_read"
BLOCKING_TOOL = "blocking_tool"
POLL_SECONDS = 0.02


def _record(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\\n")


class CountingReadTool:
    """The builtin read tool's behaviour, recorded, under its own tool name."""

    def __init__(self, gate: Path) -> None:
        self._inner = ReadTool()
        self._gate = gate
        self.definition = ToolDefinition(
            name=COUNTING_READ_TOOL,
            description="Read a file inside the workspace and record the execution.",
            read_only=True,
        )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _record(self._gate / "counting_read.executions", str(os.getpid()))
        return replace(self._inner.invoke(call, workspace=workspace), tool_name=self.definition.name)


class BlockingTool:
    """Records its call id, then blocks until the release marker exists."""

    definition = ToolDefinition(name=BLOCKING_TOOL, description="Blocks until released.", read_only=False)

    def __init__(self, gate: Path) -> None:
        self._gate = gate

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        _record(self._gate / "blocking_tool.calls", f"{os.getpid()}:{call.tool_call_id}")
        (self._gate / "entered.txt").write_text("entered", encoding="utf-8")
        while not (self._gate / "release.txt").exists():
            time.sleep(POLL_SECONDS)
        return ToolResult(tool_name=self.definition.name, status="ok", content="released")


class Step:
    def __init__(self, *, tool_call: Any = None, output: str | None = None, is_finished: bool = False) -> None:
        self.events: tuple[object, ...] = ()
        self.tool_call = tool_call
        self.output = output
        self.is_finished = is_finished


class Graph:
    """Read first, then block inside a tool call, then finish.

    ``read_first=False`` starts with the blocking call, so no tool result exists
    before the crash and no safe-boundary checkpoint is ever captured.
    ``safe_boundary=False`` never reports a safe boundary, which is what a graph
    without ``is_at_safe_boundary`` looks like.
    """

    def __init__(self, *, read_first: bool = True, safe_boundary: bool = True) -> None:
        self._read_first = read_first
        self._safe_boundary = safe_boundary

    def is_at_safe_boundary(self) -> bool:
        return self._safe_boundary

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> Step:
        _ = request, session
        if self._read_first:
            if len(tool_results) == 0:
                return Step(tool_call=ToolCall(tool_name=COUNTING_READ_TOOL, arguments={"path": "sample.txt"}))
            if len(tool_results) == 1:
                return Step(tool_call=ToolCall(tool_name=BLOCKING_TOOL, arguments={}))
            return Step(output="done", is_finished=True)
        if len(tool_results) == 0:
            return Step(tool_call=ToolCall(tool_name=BLOCKING_TOOL, arguments={}))
        return Step(output="done", is_finished=True)


def build_runtime(workspace: Path, gate: Path, *, read_first: bool = True, safe_boundary: bool = True) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=workspace,
        tool_registry=ToolRegistry.from_tools([CountingReadTool(gate), BlockingTool(gate)]),
        graph=Graph(read_first=read_first, safe_boundary=safe_boundary),
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False), execution_engine="deterministic", approval_mode="allow"),
        permission_policy=PermissionPolicy(mode="allow"),
    )


def main() -> None:
    workspace = Path(sys.argv[1])
    gate = Path(sys.argv[2])
    read_first = sys.argv[3] == "read_first"
    runtime = build_runtime(workspace, gate, read_first=read_first)
    list(runtime.run_stream(RuntimeRequest(prompt="crash during a tool call", session_id=SESSION_ID)))


if __name__ == "__main__":
    main()
'''


def _child_module(tmp_path: Path) -> ModuleType:
    """Materialise the child script and import it, so both sides share one harness."""
    script = tmp_path / "tool_crash_child.py"
    script.write_text(_CRASH_CHILD_SOURCE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("tool_crash_child", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wait_for_marker(path: Path, *, timeout: float = 60.0) -> bool:
    """Bounded wait for an explicit signal file (never a sleep used as proof)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def _wait_for(path: Path, *, timeout: float = 60.0) -> None:
    """Bounded wait that fails the test when the signal file never appears."""
    assert _wait_for_marker(path, timeout=timeout), f"the child never produced {path.name}"


def _crash_child(workspace: Path, gate: Path, script: Path, *, read_first: bool) -> None:
    """Run the child until it is provably inside its tool call, then kill it hard."""
    process = subprocess.Popen(
        [sys.executable, str(script), str(workspace), str(gate), "read_first" if read_first else "blocking_first"],
        env=dict(os.environ),
    )
    try:
        _wait_for(gate / "blocking_tool.calls")
    finally:
        process.kill()
        process.wait(timeout=30.0)
    assert process.returncode == -signal.SIGKILL, f"the child did not die from SIGKILL: {process.returncode}"


def _session(workspace: Path, session_id: str) -> Any:
    return SqliteSessionStore().load_session(workspace=workspace, session_id=session_id)


def _entries(workspace: Path, session_id: str) -> list[str]:
    return [f"{event.sequence}:{event.event_type}" for event in _session(workspace, session_id).events]


def _tool_call_ids(workspace: Path, session_id: str, event_type: str, tool_name: str) -> list[object]:
    return [
        event.payload.get("tool_call_id")
        for event in _session(workspace, session_id).events
        if event.event_type == event_type and event.payload.get("tool") == tool_name
    ]


def test_process_crash_during_a_tool_call_resumes_without_replaying_or_claiming_it(tmp_path: Path) -> None:
    """A hard-killed run leaves honest truth, and the resume neither replays nor claims the call."""
    workspace = tmp_path / "ws"
    gate = tmp_path / "gate"
    workspace.mkdir()
    gate.mkdir()
    _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
    child = _child_module(tmp_path)

    _crash_child(workspace, gate, child.__file__, read_first=True)

    crashed_call_id = cast(str, (gate / "blocking_tool.calls").read_text(encoding="utf-8").splitlines()[-1].split(":", 1)[1])
    assert crashed_call_id.startswith("runtime-tool-")

    # Persisted truth of the crashed run: interrupted row, the completed read
    # durable once, and the in-flight call started-but-unfinished with a pending
    # intent that forbids automatic replay.
    stored = _session(workspace, child.SESSION_ID)
    assert stored.session.status == "interrupted"
    assert _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_started", child.BLOCKING_TOOL) == [crashed_call_id]
    completed = [(event.payload.get("tool"), event.payload.get("status")) for event in stored.events if event.event_type == "runtime.tool_completed"]
    assert completed == [(child.COUNTING_READ_TOOL, "ok")]
    assert _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_completed", child.BLOCKING_TOOL) == []
    read_call_id = _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_completed", child.COUNTING_READ_TOOL)
    pending_intent = cast(dict[str, object], cast(dict[str, object], stored.session.metadata["runtime_state"])["pending_tool_intent"])
    assert pending_intent == {
        "tool_call_id": crashed_call_id,
        "tool_name": child.BLOCKING_TOOL,
        "arguments": {},
        "replay_policy": "never",
        "status": "pending",
    }

    checkpoint = cast(dict[str, object], SqliteSessionStore().load_resume_checkpoint(workspace=workspace, session_id=child.SESSION_ID))
    assert checkpoint["kind"] == "interrupted"
    checkpoint_tool_results = cast(list[dict[str, object]], checkpoint["tool_results"])
    assert [result["tool_name"] for result in checkpoint_tool_results] == [child.COUNTING_READ_TOOL]
    read_completion = next(event for event in stored.events if event.event_type == "runtime.tool_completed")
    assert checkpoint["last_event_sequence"] == read_completion.sequence

    # The resume runs the same turn again from the durable tool results.
    (gate / "release.txt").write_text("released", encoding="utf-8")
    resumed = child.build_runtime(workspace, gate).resume(child.SESSION_ID)
    assert resumed.session.status == "completed"
    assert resumed.output == "done"

    final_completed = [
        (event.payload.get("tool"), event.payload.get("tool_call_id"))
        for event in _session(workspace, child.SESSION_ID).events
        if event.event_type == "runtime.tool_completed"
    ]
    # Each tool call is completed exactly once — the completed read was not
    # re-executed (same call, same id), and the crashed call is not the call the
    # resume completed (the resumed run issues a fresh invocation).
    assert [tool for tool, _ in final_completed] == [child.COUNTING_READ_TOOL, child.BLOCKING_TOOL]
    assert _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_completed", child.COUNTING_READ_TOOL) == read_call_id
    resumed_call_id = cast(str, final_completed[1][1])
    assert resumed_call_id != crashed_call_id
    # The crashed call left no trace in the resumed truth: its orphaned tail was
    # truncated and no event ever claims its id.
    assert _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_started", child.BLOCKING_TOOL) == [resumed_call_id]
    assert crashed_call_id not in _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_completed", child.BLOCKING_TOOL)

    # Execution evidence across both processes: the read ran once, the blocking
    # tool ran once in the killed process and once more for the resumed call.
    read_executions = (gate / "counting_read.executions").read_text(encoding="utf-8").splitlines()
    assert len(read_executions) == 1
    blocking_calls = (gate / "blocking_tool.calls").read_text(encoding="utf-8").splitlines()
    assert len(blocking_calls) == 2
    assert blocking_calls[0].endswith(crashed_call_id)
    assert blocking_calls[1].endswith(resumed_call_id)


def test_the_checkpoint_records_the_capability_binding_before_the_first_tool_result(tmp_path: Path) -> None:
    """A run interrupted during its first tool call is resumable: its checkpoint records the capability binding.

    The run-start checkpoint is written before the run materializes its capability
    binding, so an interruption between the two used to leave a session whose
    resume cannot be honoured. The checkpoint this test reads is the one a resume
    would use while the tool call is still in flight.
    """
    workspace = tmp_path / "ws"
    gate = tmp_path / "gate"
    workspace.mkdir()
    gate.mkdir()
    _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
    child = _child_module(tmp_path)
    runtime = child.build_runtime(workspace, gate, read_first=False)
    chunks: list[Any] = []
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            chunks.extend(runtime.run_stream(child.RuntimeRequest(prompt="go", session_id=child.SESSION_ID)))
        except BaseException as exc:  # noqa: BLE001 - the test asserts on the recorded error
            errors.append(exc)

    execution = threading.Thread(target=_run, name="first-tool-call", daemon=True)
    execution.start()
    assert _wait_for_marker(gate / "blocking_tool.calls"), "the run never reached its first tool call"

    # No tool result exists yet, so this checkpoint is exactly what a resume
    # would replay from if the process died now.
    checkpoint = cast(dict[str, object], SqliteSessionStore().load_resume_checkpoint(workspace=workspace, session_id=child.SESSION_ID))
    assert checkpoint["kind"] == "interrupted"
    assert checkpoint["tool_results"] == []
    recorded = cast(dict[str, object], checkpoint["session_metadata"])
    assert isinstance(recorded.get("agent_capability_snapshot"), dict), "the checkpoint cannot be replayed without a capability binding"
    assert cast(int, checkpoint["last_event_sequence"]) > 0

    stored = _session(workspace, child.SESSION_ID)
    assert [event.event_type for event in stored.events if event.event_type == "runtime.tool_completed"] == []

    (gate / "release.txt").write_text("released", encoding="utf-8")
    execution.join(timeout=20.0)
    assert not execution.is_alive()
    assert errors == [], errors
    assert chunks[-1].session.status == "completed"


def test_resume_without_a_recorded_capability_binding_is_refused_by_name_and_rewrites_nothing(tmp_path: Path) -> None:
    """The residual window: a checkpoint with no capability binding is refused by name, before any rewrite.

    A run interrupted before it materialized its binding leaves no capability
    record to replay, so the resume cannot be honoured. The refusal must name that
    reason and must happen before the tail rewrite: the transcript of the
    interrupted run stays exactly as it was.
    """
    workspace = tmp_path / "ws"
    gate = tmp_path / "gate"
    workspace.mkdir()
    gate.mkdir()
    _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
    _ = (gate / "release.txt").write_text("released", encoding="utf-8")
    child = _child_module(tmp_path)
    runtime = child.build_runtime(workspace, gate, read_first=False)
    assert runtime.run(child.RuntimeRequest(prompt="go", session_id=child.SESSION_ID)).session.status == "completed"

    # Rebuild the checkpoint of a run that was interrupted before it materialized
    # its binding: the real session metadata, minus the capability record.
    store = SqliteSessionStore()
    metadata = dict(_session(workspace, child.SESSION_ID).session.metadata)
    assert metadata.pop("agent_capability_snapshot", None) is not None
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=child.SESSION_ID,
        prompt="go",
        session_metadata=metadata,
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=False,
    )
    before_events = _entries(workspace, child.SESSION_ID)
    before_checkpoint = store.load_resume_checkpoint(workspace=workspace, session_id=child.SESSION_ID)

    with pytest.raises(RuntimeRequestError, match="records no agent capability snapshot"):
        runtime.resume(child.SESSION_ID)

    assert _entries(workspace, child.SESSION_ID) == before_events
    assert store.load_resume_checkpoint(workspace=workspace, session_id=child.SESSION_ID) == before_checkpoint
    assert _session(workspace, child.SESSION_ID).session.status == "interrupted"


def test_crash_before_the_first_safe_boundary_resumes_without_claiming_the_crashed_call(tmp_path: Path) -> None:
    """A crash during the first tool call is resumable, and the crashed call is never claimed completed.

    No tool result existed at the crash, so no safe-boundary checkpoint was ever
    captured: the checkpoint a resume uses is the one the run wrote once its
    capability binding was materialized. The crashed invocation is neither
    completed nor replayed as such — its orphaned tail is dropped and the resumed
    run issues a fresh call with a new id, while the pending intent that forbids
    automatic replay is recorded for the window in which it existed.
    """
    workspace = tmp_path / "ws"
    gate = tmp_path / "gate"
    workspace.mkdir()
    gate.mkdir()
    _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
    child = _child_module(tmp_path)

    _crash_child(workspace, gate, child.__file__, read_first=False)

    crashed_call_id = cast(str, (gate / "blocking_tool.calls").read_text(encoding="utf-8").splitlines()[-1].split(":", 1)[1])
    stored = _session(workspace, child.SESSION_ID)
    assert stored.session.status == "interrupted"
    # No tool result was ever committed, and the in-flight call is not completed.
    assert [event.event_type for event in stored.events if event.event_type == "runtime.tool_completed"] == []
    assert _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_started", child.BLOCKING_TOOL) == [crashed_call_id]
    pending_intent = cast(dict[str, object], cast(dict[str, object], stored.session.metadata["runtime_state"])["pending_tool_intent"])
    assert pending_intent["tool_call_id"] == crashed_call_id
    assert (pending_intent["status"], pending_intent["replay_policy"]) == ("pending", "never")

    checkpoint = cast(dict[str, object], SqliteSessionStore().load_resume_checkpoint(workspace=workspace, session_id=child.SESSION_ID))
    assert checkpoint["kind"] == "interrupted"
    assert checkpoint["tool_results"] == []
    checkpoint_metadata = cast(dict[str, object], checkpoint["session_metadata"])
    assert isinstance(checkpoint_metadata.get("agent_capability_snapshot"), dict)
    assert cast(int, checkpoint["last_event_sequence"]) > 0

    (gate / "release.txt").write_text("released", encoding="utf-8")
    resumed = child.build_runtime(workspace, gate, read_first=False).resume(child.SESSION_ID)
    assert resumed.session.status == "completed"
    assert resumed.output == "done"

    final_completed = [
        (event.payload.get("tool"), event.payload.get("tool_call_id"))
        for event in _session(workspace, child.SESSION_ID).events
        if event.event_type == "runtime.tool_completed"
    ]
    assert [tool for tool, _ in final_completed] == [child.BLOCKING_TOOL]
    resumed_call_id = cast(str, final_completed[0][1])
    assert resumed_call_id != crashed_call_id
    # The crashed invocation left no trace in the resumed truth: its orphaned tail
    # was dropped and no event claims its id, while the resumed call is reported
    # exactly once as the call that did complete.
    assert _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_started", child.BLOCKING_TOOL) == [resumed_call_id]
    assert _tool_call_ids(workspace, child.SESSION_ID, "runtime.tool_completed", child.BLOCKING_TOOL) == [resumed_call_id]
    blocking_calls = (gate / "blocking_tool.calls").read_text(encoding="utf-8").splitlines()
    assert len(blocking_calls) == 2
    assert blocking_calls[0].endswith(crashed_call_id)
    assert blocking_calls[1].endswith(resumed_call_id)


def test_streaming_resume_on_a_settled_crash_completes_the_turn(tmp_path: Path) -> None:
    """The streaming resume path (TUI session switch, CLI `sessions resume`) recovers a settled crash.

    A streaming resume registers its own run for the session before entering the
    coordinator, so the ownership guard must exclude that handle: refusing it
    would make every streaming resume fail while the blocking form works.
    """
    workspace = tmp_path / "ws"
    gate = tmp_path / "gate"
    workspace.mkdir()
    gate.mkdir()
    _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
    child = _child_module(tmp_path)

    _crash_child(workspace, gate, child.__file__, read_first=True)
    crashed_call_id = cast(str, (gate / "blocking_tool.calls").read_text(encoding="utf-8").splitlines()[-1].split(":", 1)[1])
    assert _session(workspace, child.SESSION_ID).session.status == "interrupted"

    (gate / "release.txt").write_text("released", encoding="utf-8")
    runtime = child.build_runtime(workspace, gate)
    chunks = list(runtime.resume_stream(child.SESSION_ID))

    assert chunks, "the streaming resume produced no chunks"
    assert chunks[-1].session.status == "completed"
    assert [chunk.output for chunk in chunks if chunk.kind == "output"] == ["done"]
    final_completed = [
        (event.payload.get("tool"), event.payload.get("tool_call_id"))
        for event in _session(workspace, child.SESSION_ID).events
        if event.event_type == "runtime.tool_completed"
    ]
    assert [tool for tool, _ in final_completed] == [child.COUNTING_READ_TOOL, child.BLOCKING_TOOL]
    assert cast(str, final_completed[-1][1]) != crashed_call_id
    assert all(event.payload.get("tool_call_id") != crashed_call_id for event in _session(workspace, child.SESSION_ID).events)
