from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import (
    RuntimeProviderContextSegmentSnapshot,
    RuntimeRequest,
    RuntimeResponse,
)
from voidcode.runtime.events import RUNTIME_TOOL_PROGRESS
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools import ReadTool, ShellExecTool, tool_output_artifact_temp_root
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult
from voidcode.tools.runtime_context import (
    RuntimeToolInvocationContext,
    bind_runtime_tool_context,
    current_runtime_tool_context,
)


class _AbortSignal:
    def __init__(self, *, cancelled: bool = False, reason: str | None = None) -> None:
        self._cancelled = cancelled
        self.reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self, reason: str | None = None) -> None:
        self._cancelled = True
        self.reason = reason


class _InstantTool:
    definition = ToolDefinition(name="instant_tool", description="Returns instantly.")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        return ToolResult(tool_name=self.definition.name, status="ok", content="done")


class _LargeOutputTool:
    definition = ToolDefinition(name="large_output_tool", description="Returns large output.")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="".join(f"line-{index}\n" for index in range(2100)),
        )


class _SensitiveContextTool:
    definition = ToolDefinition(name="sensitive_context_tool", description="Returns metadata.")

    def __init__(self, *, data_uri: str, raw_data_content: str) -> None:
        self._data_uri = data_uri
        self._raw_data_content = raw_data_content

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="metadata captured",
            data={
                "arguments": {"content": self._raw_data_content},
                "attachment": {"mime": "image/png", "data_uri": self._data_uri},
                "status": "tool-data-status-must-not-win",
            },
        )


class _HangingTool:
    definition = ToolDefinition(name="hanging_tool", description="Never returns.")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        time.sleep(9999)
        return ToolResult(tool_name=self.definition.name, status="ok", content="unreachable")


_CANCEL_POLL_SECONDS = 0.01
_FIRST_WRITE = "first-write.txt"
_SECOND_WRITE = "second-write.txt"


class _CancellationWriterTool:
    """Controllable mutating tool that polls the runtime cancellation signal.

    Every behaviour writes once, then blocks until the runtime cancellation
    signal is observable. The tool then either stops (``stop``), ignores the
    signal until the test releases it (``ignore``), or finishes immediately with
    an ``ok`` result (``complete``). Nothing waits on a sleep race: the runtime
    only cancels after its deadline expired, and ``ignore`` stays alive until
    the test explicitly releases it.
    """

    definition = ToolDefinition(
        name="cancellation_writer_tool",
        description="Writes a file, blocks on runtime cancellation, then acts on the signal.",
        read_only=False,
    )

    def __init__(self, workspace: Path, *, behaviour: Literal["stop", "ignore", "complete"]) -> None:
        self._workspace = workspace
        self._behaviour = behaviour
        self.started = threading.Event()
        self.cancellation_observed = threading.Event()
        self.release = threading.Event()
        self.late_write_done = threading.Event()
        self.finished = threading.Event()
        self.cancelled_at_end = False
        self.second_write_performed = False

    def _observe_cancellation(self) -> bool:
        context = current_runtime_tool_context()
        signal = context.abort_signal if context is not None else None
        if signal is None or not signal.cancelled:
            return False
        self.cancellation_observed.set()
        return True

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call
        self.started.set()
        (self._workspace / _FIRST_WRITE).write_text("first\n", encoding="utf-8")
        if self._behaviour == "ignore":
            while True:
                self._observe_cancellation()
                if self.release.wait(_CANCEL_POLL_SECONDS):
                    break
        else:
            while not self._observe_cancellation():
                time.sleep(_CANCEL_POLL_SECONDS)
        self.cancelled_at_end = True
        if self._behaviour == "stop":
            self.finished.set()
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                content="cancelled by the runtime before the second write",
                error="cancelled by the runtime before the second write",
                data={"cancelled": True, "second_write_performed": False},
            )
        if self._behaviour == "ignore":
            (self._workspace / _SECOND_WRITE).write_text("second\n", encoding="utf-8")
            self.second_write_performed = True
            self.late_write_done.set()
            self.finished.set()
            return ToolResult(tool_name=self.definition.name, status="ok", content="wrote both files")
        self.finished.set()
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="finished at the boundary",
            data={"completed_at_boundary": True, "second_write_performed": False},
        )


class _SlowButFinishingTool:
    definition = ToolDefinition(name="slow_but_finishing_tool", description="Finishes after a short sleep.")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        time.sleep(0.05)
        return ToolResult(tool_name=self.definition.name, status="ok", content="finished")


class _ToolNativeTimeoutErrorTool:
    definition = ToolDefinition(
        name="tool_native_timeout_error_tool",
        description="Raises a tool-native TimeoutError.",
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        raise TimeoutError("tool-native timeout")


class _FatalExceptionTool:
    definition = ToolDefinition(
        name="fatal_exception_tool",
        description="Raises a non-timeout fatal exception.",
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        raise ValueError("fatal tool error")


@dataclass(frozen=True, slots=True)
class _StaticGraphStep:
    tool_call: ToolCall | None
    output: str | None
    events: tuple[Any, ...] = ()
    is_finished: bool = False
    reasoning: str | None = None


class _SingleToolCallWithArgumentsGraph:
    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self._tool_name = tool_name
        self._arguments = arguments
        self.seen_tool_results: tuple[ToolResult, ...] = ()

    def step(
        self,
        request: Any,
        tool_results: tuple[ToolResult, ...],
        *,
        session: Any,
    ) -> Any:
        _ = request, session
        self.seen_tool_results = tool_results
        if not tool_results:
            return _StaticGraphStep(
                tool_call=ToolCall(
                    tool_name=self._tool_name,
                    arguments=self._arguments,
                    tool_call_id="sensitive-context-call",
                ),
                output=None,
            )
        return _StaticGraphStep(tool_call=None, output="completed", is_finished=True)


class _SingleToolCallGraph:
    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._done = False

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> Any:
        if tool_results:
            self._done = True

        class _Step:
            reasoning: str | None = None

        step = _Step()

        if not tool_results:
            step.tool_call = ToolCall(tool_name=self._tool_name, arguments={})  # type: ignore[attr-defined]
            step.output = None  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = False  # type: ignore[attr-defined]
        else:
            step.tool_call = None  # type: ignore[attr-defined]
            step.output = "completed"  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = True  # type: ignore[attr-defined]

        return step


class _ShellExecGraph:
    def __init__(self, arguments: dict[str, object]) -> None:
        self._arguments = arguments

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> Any:
        _ = request, session

        class _Step:
            reasoning: str | None = None

        step = _Step()
        if not tool_results:
            step.tool_call = ToolCall(tool_name="shell_exec", arguments=self._arguments)  # type: ignore[attr-defined]
            step.output = None  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = False  # type: ignore[attr-defined]
        else:
            step.tool_call = None  # type: ignore[attr-defined]
            step.output = "completed"  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = True  # type: ignore[attr-defined]
        return step


def _collect_events(runtime: VoidCodeRuntime, prompt: str = "go") -> list[str]:
    chunks = list(runtime.run_stream(RuntimeRequest(prompt=prompt)))
    return [c.event.event_type for c in chunks if c.kind == "event" and c.event is not None]


def _make_runtime(
    tmp_path: Path,
    tool: Any,
    *,
    tool_timeout_seconds: int | None = None,
) -> VoidCodeRuntime:
    registry = ToolRegistry.from_tools([tool])
    config = RuntimeConfig(
        mcp=RuntimeMcpConfig(enabled=False),
        execution_engine="deterministic",
        tool_timeout_seconds=tool_timeout_seconds,
    )
    return VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=registry,
        graph=_SingleToolCallGraph(tool.definition.name),
        config=config,
    )


def test_tool_completes_normally_within_timeout(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, _InstantTool(), tool_timeout_seconds=10)
    event_types = _collect_events(runtime)

    assert "runtime.tool_completed" in event_types
    assert "runtime.tool_timeout" not in event_types
    assert "runtime.failed" not in event_types


def test_shell_exec_returns_interrupted_result_when_runtime_abort_is_set(tmp_path: Path) -> None:
    tool = ShellExecTool()
    command = f'"{sys.executable}" -c "import time; time.sleep(10)"'

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="shell-abort",
            abort_signal=_AbortSignal(cancelled=True, reason="test abort"),
        )
    ):
        result = tool.invoke(
            ToolCall(tool_name="shell_exec", arguments={"command": command, "timeout": 30}),
            workspace=tmp_path,
        )

    assert result.status == "error"
    assert result.data["interrupted"] is True
    assert result.data["cancelled"] is True
    assert result.data["reason"] == "test abort"


def test_slow_tool_that_finishes_within_timeout_is_not_interrupted(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, _SlowButFinishingTool(), tool_timeout_seconds=10)
    event_types = _collect_events(runtime)

    assert "runtime.tool_completed" in event_types
    assert "runtime.tool_timeout" not in event_types
    assert "runtime.failed" not in event_types


def test_hanging_tool_does_not_emit_runtime_tool_timeout(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, _HangingTool(), tool_timeout_seconds=1)

    start = time.monotonic()
    iterator = runtime.run_stream(RuntimeRequest(prompt="go"))
    first_chunks: list[Any] = []
    for _ in range(3):
        first_chunks.append(next(iterator))
    elapsed = time.monotonic() - start
    event_types = [chunk.event.event_type for chunk in first_chunks if chunk.kind == "event" and chunk.event is not None]

    assert elapsed < 5
    assert "runtime.tool_timeout" not in event_types
    assert "runtime.failed" not in event_types


def test_hanging_tool_does_not_emit_tool_completed(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, _HangingTool(), tool_timeout_seconds=1)

    iterator = runtime.run_stream(RuntimeRequest(prompt="go"))
    first_chunks = [next(iterator) for _ in range(3)]
    event_types = [chunk.event.event_type for chunk in first_chunks if chunk.kind == "event" and chunk.event is not None]

    assert "runtime.tool_completed" not in event_types


def test_timeout_event_payload_contains_tool_name_and_seconds(tmp_path: Path) -> None:
    timeout = 1
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph(
            {
                "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                "timeout": 10,
            }
        ),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=timeout,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    timeout_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.tool_timeout"]

    assert len(timeout_events) == 1
    payload = timeout_events[0].payload
    assert payload["tool"] == "shell_exec"
    assert payload["timeout_seconds"] == timeout


def test_shell_exec_progress_streams_before_tool_completion(tmp_path: Path) -> None:
    marker = tmp_path / "command-finished.txt"
    command = (
        f'"{sys.executable}" -c "from pathlib import Path; import sys, time; '
        "sys.stdout.write('alpha\\n'); sys.stdout.flush(); "
        "time.sleep(0.5); "
        "Path('command-finished.txt').write_text('done', encoding='utf-8'); "
        "sys.stdout.write('omega\\n'); sys.stdout.flush()"
        '"'
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph({"command": command, "timeout": 5}),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
        ),
    )

    chunks: list[Any] = []
    iterator = runtime.run_stream(RuntimeRequest(prompt="go"))
    first_progress = None
    for chunk in iterator:
        chunks.append(chunk)
        if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == RUNTIME_TOOL_PROGRESS:
            first_progress = chunk.event
            break

    assert first_progress is not None
    assert marker.exists() is False
    assert first_progress.payload["tool"] == "shell_exec"
    assert first_progress.payload["stream"] == "stdout"
    assert first_progress.payload["chunk"] == "alpha\n"
    assert isinstance(first_progress.payload["run_id"], str)
    assert first_progress.payload["invocation_id"] == first_progress.payload["tool_call_id"]

    chunks.extend(iterator)
    event_types = [c.event.event_type for c in chunks if c.kind == "event" and c.event is not None]
    assert event_types.index("runtime.tool_started") < event_types.index(RUNTIME_TOOL_PROGRESS)
    assert event_types.index(RUNTIME_TOOL_PROGRESS) < event_types.index("runtime.tool_completed")
    progress_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == RUNTIME_TOOL_PROGRESS]
    assert progress_events
    assert len({event.payload["run_id"] for event in progress_events}) == 1
    assert len({event.payload["invocation_id"] for event in progress_events}) == 1
    assert [int(event.payload["ordinal"]) for event in progress_events] == sorted(int(event.payload["ordinal"]) for event in progress_events)
    completed = next(c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.tool_completed")
    assert completed.payload["tool_call_id"] == first_progress.payload["invocation_id"]
    assert completed.payload["stdout"] == "alpha\nomega\n"


def test_shell_exec_runtime_timeout_preserves_partial_progress_and_final_output(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph(
            {
                "command": (f'"{sys.executable}" -c "import sys, time; sys.stdout.write(\'partial\\n\'); sys.stdout.flush(); time.sleep(2)"'),
                "timeout": 10,
            }
        ),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    progress_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == RUNTIME_TOOL_PROGRESS]
    completed = next(c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.tool_completed")

    assert [event.payload["chunk"] for event in progress_events] == ["partial\n"]
    assert completed.payload["status"] == "error"
    assert completed.payload["stdout"] == "partial\n"
    assert completed.payload["interrupted"] is True
    assert completed.payload["timed_out"] is True
    assert isinstance(progress_events[0].payload["run_id"], str)
    assert progress_events[0].payload["invocation_id"] == completed.payload["tool_call_id"]
    assert progress_events[0].payload["ordinal"] == 1


def test_runtime_does_not_hang_after_tool_timeout(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph(
            {
                "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                "timeout": 10,
            }
        ),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    start = time.monotonic()
    _collect_events(runtime)
    elapsed = time.monotonic() - start

    assert elapsed < 5


def test_runtime_timeout_unset_does_not_emit_runtime_tool_timeout(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, _InstantTool(), tool_timeout_seconds=None)
    event_types = _collect_events(runtime)

    assert "runtime.tool_completed" in event_types
    assert "runtime.tool_timeout" not in event_types


def test_runtime_caps_large_tool_output_before_feedback(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, _LargeOutputTool(), tool_timeout_seconds=None)

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) == 1
    payload = completed_events[0].payload
    assert payload["truncated"] is True
    assert isinstance(payload["output_path"], str)
    output_path = Path(payload["output_path"]).resolve()
    artifact_root = tool_output_artifact_temp_root().resolve()
    assert artifact_root in output_path.parents
    assert payload["artifact_missing"] is False
    diagnostics = payload["diagnostics"]
    assert isinstance(diagnostics, list)
    assert diagnostics[-1]["retry_guidance"] == (f'Read the full output with read(path="voidcode://artifact/{payload["artifact_id"]}").')
    assert isinstance(payload["artifact_id"], str)
    artifact = payload["artifact"]
    assert isinstance(artifact, dict)
    assert artifact["session_id"] == completed_events[0].session_id
    assert artifact["tool_call_id"] == payload["tool_call_id"]
    assert isinstance(payload["content"], str)
    assert "Tool output truncated" in payload["content"]
    assert f"artifact_id={payload['artifact_id']}" in payload["content"]
    assert "line-2099" not in payload["content"]
    assert output_path.read_text(encoding="utf-8").endswith("line-2099\n")
    assert not (tmp_path / ".voidcode" / "tool-output").exists()


def test_runtime_resolves_tool_output_artifacts_and_reports_missing_debug_state(
    tmp_path: Path,
) -> None:
    session_id = "artifact-resolver-session"
    runtime = _make_runtime(tmp_path, _LargeOutputTool(), tool_timeout_seconds=None)

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))
    replay = runtime.resume(session_id)
    completed_event = next(event for event in replay.events if event.event_type == "runtime.tool_completed")
    artifact_id = completed_event.payload["artifact_id"]
    tool_call_id = completed_event.payload["tool_call_id"]
    assert isinstance(artifact_id, str)
    assert isinstance(tool_call_id, str)

    metadata = runtime.resolve_tool_output_artifact(
        session_id=session_id,
        artifact_id=artifact_id,
    )
    assert metadata["status"] == "available"
    assert metadata["artifact_missing"] is False

    read_result = runtime.read_tool_output_artifact(
        session_id=session_id,
        tool_call_id=tool_call_id,
        offset=2099,
        limit=1,
    )
    assert read_result["content"] == "line-2099\n"
    search_result = runtime.search_tool_output_artifact(
        session_id=session_id,
        artifact_id=artifact_id,
        pattern="line-2099",
    )
    assert search_result["match_count"] == 1

    artifact = completed_event.payload["artifact"]
    assert isinstance(artifact, dict)
    Path(cast(str, artifact["path"])).unlink()

    missing = runtime.resolve_tool_output_artifact(
        session_id=session_id,
        artifact_id=artifact_id,
    )
    assert missing["status"] == "missing"
    assert missing["artifact_missing"] is True
    missing_read = runtime.read_tool_output_artifact(
        session_id=session_id,
        artifact_id=artifact_id,
    )
    assert missing_read["status"] == "missing"
    snapshot = runtime.session_debug_snapshot(session_id=session_id)
    assert snapshot.last_tool is not None
    assert snapshot.last_tool.artifact["artifact_id"] == artifact_id
    assert snapshot.last_tool.artifact["status"] == "missing"
    assert snapshot.last_tool.artifact["artifact_missing"] is True
    assert snapshot.provider_context is not None
    artifact_segments: list[RuntimeProviderContextSegmentSnapshot] = []
    for segment in snapshot.provider_context.segments:
        segment_data = segment.metadata.get("data")
        if not isinstance(segment_data, dict):
            continue
        typed_segment_data = cast(dict[str, object], segment_data)
        if typed_segment_data.get("artifact_id") == artifact_id:
            artifact_segments.append(segment)
    assert artifact_segments
    segment_data = artifact_segments[-1].metadata["data"]
    assert isinstance(segment_data, dict)
    assert segment_data["artifact_missing"] is True


def test_runtime_artifact_resolver_skips_invalid_candidate_for_same_tool_call(
    tmp_path: Path,
) -> None:
    source_runtime = _make_runtime(tmp_path, _LargeOutputTool(), tool_timeout_seconds=None)
    _ = list(source_runtime.run_stream(RuntimeRequest(prompt="go", session_id="source-session")))
    source_replay = source_runtime.resume("source-session")
    completed_event = next(event for event in source_replay.events if event.event_type == "runtime.tool_completed")
    valid_artifact = completed_event.payload["artifact"]
    assert isinstance(valid_artifact, dict)
    tool_call_id = completed_event.payload["tool_call_id"]
    assert isinstance(tool_call_id, str)
    forged_artifact = {
        **cast(dict[str, object], valid_artifact),
        "artifact_id": "artifact_",
    }
    session_id = "resolver-invalid-first-session"
    store = SqliteSessionStore()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False), execution_engine="deterministic"),
        session_store=store,
    )
    store.save_interrupted_checkpoint(
        workspace=tmp_path,
        session_id=session_id,
        prompt="go",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        create_if_missing=True,
    )
    store.append_session_events(
        workspace=tmp_path,
        session_id=session_id,
        events=(
            (
                "runtime.tool_completed",
                "tool",
                {
                    "tool": "large_output_tool",
                    "tool_call_id": tool_call_id,
                    "status": "ok",
                    "content": "forged",
                    "artifact": forged_artifact,
                },
                None,
            ),
            (
                "runtime.tool_completed",
                "tool",
                {**completed_event.payload, "tool_call_id": tool_call_id},
                None,
            ),
        ),
    )
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="go", session_id=session_id),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=session_id),
                status="completed",
                turn=1,
                metadata={},
            ),
            events=(),
            output="done",
        ),
    )
    metadata = runtime.resolve_tool_output_artifact(
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    read_result = runtime.read_tool_output_artifact(
        session_id=session_id,
        tool_call_id=tool_call_id,
        offset=2099,
        limit=1,
    )

    assert metadata["artifact_id"] == completed_event.payload["artifact_id"]
    assert metadata["status"] == "available"
    assert read_result["content"] == "line-2099\n"


def test_runtime_sanitizes_tool_arguments_and_data_before_events_and_feedback(
    tmp_path: Path,
) -> None:
    raw_argument_content = "RAW FILE CONTENT SHOULD NOT BE MODEL VISIBLE"
    raw_data_content = "RAW TOOL DATA CONTENT SHOULD NOT BE MODEL VISIBLE"
    raw_old_string = "old secret"
    raw_new_string = "new secret"
    data_uri = "data:image/png;base64," + "A" * 64
    tool = _SensitiveContextTool(data_uri=data_uri, raw_data_content=raw_data_content)
    graph = _SingleToolCallWithArgumentsGraph(
        tool.definition.name,
        {
            "path": "out.txt",
            "content": raw_argument_content,
            "edits": [{"oldString": raw_old_string, "newString": raw_new_string}],
        },
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([tool]),
        graph=graph,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False), execution_engine="deterministic"),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) == 1
    payload = completed_events[0].payload
    assert payload["status"] == "ok"
    arguments_obj = payload["arguments"]
    assert isinstance(arguments_obj, dict)
    arguments = cast(dict[str, object], arguments_obj)
    assert arguments["path"] == "out.txt"
    assert arguments["content"] == {
        "omitted": True,
        "byte_count": len(raw_argument_content.encode("utf-8")),
        "line_count": 1,
    }
    edits = arguments["edits"]
    assert isinstance(edits, list)
    edit_items = cast(list[object], edits)
    assert edit_items[0] == {
        "oldString": {
            "omitted": True,
            "byte_count": len(raw_old_string.encode("utf-8")),
            "line_count": 1,
        },
        "newString": {
            "omitted": True,
            "byte_count": len(raw_new_string.encode("utf-8")),
            "line_count": 1,
        },
    }
    attachment = payload["attachment"]
    assert isinstance(attachment, dict)
    assert attachment["data_uri"] == {
        "omitted": True,
        "byte_count": len(data_uri.encode("utf-8")),
        "line_count": 1,
    }
    completed_payload_text = str(payload)
    assert raw_argument_content not in completed_payload_text
    assert raw_data_content not in completed_payload_text
    assert raw_old_string not in completed_payload_text
    assert raw_new_string not in completed_payload_text
    assert data_uri not in completed_payload_text

    assert len(graph.seen_tool_results) == 1
    feedback_payload_text = str(graph.seen_tool_results[0].data)
    assert raw_argument_content not in feedback_payload_text
    assert raw_data_content not in feedback_payload_text
    assert data_uri not in feedback_payload_text


def test_session_status_is_failed_after_timeout(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph(
            {
                "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                "timeout": 10,
            }
        ),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    statuses = [c.session.status for c in chunks]
    assert "failed" in statuses


def test_shell_exec_uses_existing_tool_timeout_when_runtime_timeout_is_unset(
    tmp_path: Path,
) -> None:
    command = f'"{sys.executable}" -c "print(1)"'
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph({"command": command}),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=None,
        ),
    )
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) == 1
    assert completed_events[0].payload["timeout"] == 120
    assert completed_events[0].payload["cwd"] == str(tmp_path.resolve())
    assert completed_events[0].payload["exit_code"] == 0
    assert completed_events[0].payload["stdout_truncated"] is False
    assert completed_events[0].payload["stderr_truncated"] is False
    assert completed_events[0].payload["truncated"] is False


def test_shell_exec_timeout_wins_when_shorter_than_runtime_timeout(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(2)"'
    session_id = "shell-exec-local-timeout"
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph({"command": command, "timeout": 1}),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=10,
        ),
    )

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))

    replay = runtime.resume(session_id)
    event_types = [event.event_type for event in replay.events]
    completed_events = [event for event in replay.events if event.event_type == "runtime.tool_completed"]

    assert "runtime.tool_timeout" not in event_types
    assert len(completed_events) == 1
    assert completed_events[0].payload["status"] == "error"
    assert completed_events[0].payload["error"] == "shell_exec command timed out after 1s"


def test_runtime_timeout_wins_when_shorter_than_shell_exec_timeout(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(2)"'
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph({"command": command, "timeout": 10}),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    event_types = [chunk.event.event_type for chunk in chunks if chunk.kind == "event" and chunk.event is not None]
    timeout_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_timeout"
    ]

    assert "runtime.failed" in event_types
    assert len(timeout_events) == 1
    # The timeout payload additionally records the execution facts the runtime
    # verified: shell_exec stops its own process on the runtime timeout, so no
    # runtime cancellation was needed, and the execution is confirmed stopped.
    assert timeout_events[0].payload == {
        "tool": "shell_exec",
        "timeout_seconds": 1,
        "cancellation_signalled": False,
        "execution_stopped": True,
        "side_effect_state": "settled",
    }


def test_runtime_timeout_prevents_delayed_shell_exec_side_effect(tmp_path: Path) -> None:
    """A timed-out command cannot write after the runtime returns.

    The observation window is gated on an independent witness process that
    passes the moment at which the timed-out command would have written its
    side effect, so the assertion cannot pass merely because the window was too
    short (the previous version stopped waiting as soon as the file was absent).
    """
    side_effect_path = tmp_path / "late-side-effect.txt"
    command = (
        f'"{sys.executable}" -c "import time; '
        f"from pathlib import Path; "
        f"time.sleep(2); "
        f"Path({str(side_effect_path)!r}).write_text('done', encoding='utf-8')\""
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph({"command": command, "timeout": 10}),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    timeout_payload = _single_tool_event(chunks, "runtime.tool_timeout").payload

    witness_path = tmp_path / "witness.txt"
    witness = "import sys, time; from pathlib import Path; time.sleep(2.5); Path(sys.argv[1]).write_text('passed', encoding='utf-8')"
    subprocess.run([sys.executable, "-c", witness, str(witness_path)], check=True, timeout=30)
    assert witness_path.exists(), "the witness must pass the moment the timed-out command would have written"

    assert side_effect_path.exists() is False
    # shell_exec stops its own process on the runtime timeout, so the runtime
    # never had to cancel it: the payload says so instead of implying anything
    # about work that may still be running.
    assert timeout_payload["cancellation_signalled"] is False
    assert timeout_payload["execution_stopped"] is True
    assert timeout_payload["side_effect_state"] == "settled"


def _single_tool_event(chunks: list[Any], event_type: str) -> Any:
    events = [chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == event_type]
    assert len(events) == 1, f"expected exactly one {event_type} event, saw {len(events)}"
    return events[0]


def _replay_tool_events(runtime: VoidCodeRuntime, session_id: str, event_type: str) -> list[Any]:
    return [event for event in runtime.resume(session_id).events if event.event_type == event_type]


def _wait_for_log_record(caplog: pytest.LogCaptureFixture, needle: str, *, timeout: float = 10.0) -> None:
    """Wait for an asynchronous observer thread to record its diagnostic line."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in caplog.text:
            return
        time.sleep(0.01)
    raise AssertionError(f"no log record containing {needle!r} within {timeout}s; saw: {caplog.text!r}")


def _timeout_runtime(tmp_path: Path, tool: Any) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([tool]),
        graph=_SingleToolCallGraph(tool.definition.name),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )


def test_runtime_timeout_signals_cancellation_and_stops_a_cooperative_tool(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cooperative tool sees the runtime cancellation and stops on its own."""
    session_id = "timeout-cancellation-observed"
    tool = _CancellationWriterTool(tmp_path, behaviour="stop")
    runtime = _timeout_runtime(tmp_path, tool)

    with caplog.at_level(logging.WARNING, logger="voidcode.runtime.tool_execution"):
        chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))

    # The runtime cancelled the in-flight invocation on the same signal the
    # interrupt path cancels, and the tool observed it.
    assert tool.started.is_set()
    assert tool.cancellation_observed.is_set()
    assert tool.cancelled_at_end is True
    assert tool.finished.is_set() is True
    # An observed cancellation means the tool stopped: no late write, and the
    # runtime could confirm the execution stopped.
    assert tool.second_write_performed is False
    assert (tmp_path / _SECOND_WRITE).exists() is False
    assert (tmp_path / _FIRST_WRITE).read_text(encoding="utf-8") == "first\n"

    timeout_payload = _single_tool_event(chunks, "runtime.tool_timeout").payload
    assert timeout_payload == {
        "tool": tool.definition.name,
        "timeout_seconds": 1,
        "cancellation_signalled": True,
        "execution_stopped": True,
        "side_effect_state": "settled",
    }

    completed_payload = _single_tool_event(chunks, "runtime.tool_completed").payload
    assert completed_payload["status"] == "error"
    assert completed_payload["diagnostics"]["kind"] == "tool_timeout"
    assert completed_payload["side_effect_state"] == "settled"
    # The tool's own cancellation result is a late result: it must not become
    # the recorded tool result.
    assert completed_payload["content"] is None
    assert "cancelled by the runtime" not in str(completed_payload)

    failed_payload = _single_tool_event(chunks, "runtime.failed").payload
    assert failed_payload["kind"] == "tool_timeout"
    assert failed_payload["side_effect_state"] == "settled"
    assert failed_payload["error"] == f"tool '{tool.definition.name}' exceeded runtime timeout of 1s"

    # The arrival of the late cancellation result is recorded for diagnosis.
    _wait_for_log_record(caplog, "abandoned its timed-out execution")
    assert "status=error" in caplog.text

    assert runtime.resume(session_id).session.status == "failed"
    replayed = _replay_tool_events(runtime, session_id, "runtime.tool_completed")
    assert len(replayed) == 1
    assert replayed[0].payload["status"] == "error"
    assert replayed[0].payload["diagnostics"]["kind"] == "tool_timeout"


def test_runtime_timeout_reports_unknown_side_effects_when_the_tool_ignores_cancellation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tool that ignores cancellation leaves the runtime with an unknown state."""
    session_id = "timeout-cancellation-ignored"
    tool = _CancellationWriterTool(tmp_path, behaviour="ignore")
    runtime = _timeout_runtime(tmp_path, tool)

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="voidcode.runtime.tool_execution"):
        chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))
    elapsed = time.monotonic() - started

    # The runtime did not wait for the uncooperative tool: it cancelled the
    # invocation, reaped for a bounded window, and returned.
    assert tool.started.is_set()
    assert tool.cancellation_observed.is_set()
    assert tool.finished.is_set() is False
    assert elapsed < 3.0

    timeout_payload = _single_tool_event(chunks, "runtime.tool_timeout").payload
    assert timeout_payload == {
        "tool": tool.definition.name,
        "timeout_seconds": 1,
        "cancellation_signalled": True,
        "execution_stopped": False,
        "side_effect_state": "unknown",
    }

    completed_payload = _single_tool_event(chunks, "runtime.tool_completed").payload
    assert completed_payload["status"] == "error"
    assert completed_payload["side_effect_state"] == "unknown"
    assert "may still be running" in completed_payload["error"]

    failed_payload = _single_tool_event(chunks, "runtime.failed").payload
    assert failed_payload["kind"] == "tool_timeout"
    assert failed_payload["execution_stopped"] is False
    assert failed_payload["side_effect_state"] == "unknown"
    assert "may still be running" in failed_payload["error"]
    assert (tmp_path / _SECOND_WRITE).exists() is False

    # Releasing the tool proves the runtime's claim: the abandoned execution was
    # still in flight and performed its second write after the run returned.
    tool.release.set()
    assert tool.late_write_done.wait(10.0), "the released tool must complete"
    assert tool.second_write_performed is True
    assert (tmp_path / _SECOND_WRITE).exists() is True

    # The late completion is recorded for diagnosis, never committed as the
    # tool result.
    _wait_for_log_record(caplog, "abandoned its timed-out execution")
    assert "status=ok" in caplog.text
    replayed = _replay_tool_events(runtime, session_id, "runtime.tool_completed")
    assert len(replayed) == 1
    assert replayed[0].payload["status"] == "error"
    assert replayed[0].payload["diagnostics"]["kind"] == "tool_timeout"
    assert "wrote both files" not in str(replayed[0].payload)


def test_late_tool_completion_at_the_boundary_is_not_committed_as_the_tool_result(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A completion racing the abandoned wait is recorded, not committed."""
    session_id = "timeout-boundary-completion"
    tool = _CancellationWriterTool(tmp_path, behaviour="complete")
    runtime = _timeout_runtime(tmp_path, tool)

    with caplog.at_level(logging.WARNING, logger="voidcode.runtime.tool_execution"):
        chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))

    # The tool finished as soon as cancellation was signalled: it exited within
    # the bounded reap window, so the runtime could confirm the execution
    # stopped without reaping it by waiting.
    assert tool.cancellation_observed.is_set()
    assert tool.finished.is_set() is True

    completed_payload = _single_tool_event(chunks, "runtime.tool_completed").payload
    assert completed_payload["status"] == "error"
    assert completed_payload["execution_stopped"] is True
    assert completed_payload["side_effect_state"] == "settled"
    assert completed_payload["diagnostics"]["kind"] == "tool_timeout"
    assert completed_payload["content"] is None
    assert "finished at the boundary" not in str(completed_payload)

    _wait_for_log_record(caplog, "abandoned its timed-out execution")
    assert "status=ok" in caplog.text

    # Storage keeps the timeout outcome: the late ok result never overwrites it.
    assert runtime.resume(session_id).session.status == "failed"
    replayed = _replay_tool_events(runtime, session_id, "runtime.tool_completed")
    assert len(replayed) == 1
    assert replayed[0].payload["status"] == "error"
    assert replayed[0].payload["diagnostics"]["kind"] == "tool_timeout"
    assert "finished at the boundary" not in str(replayed[0].payload)


def test_tool_native_timeout_error_does_not_emit_runtime_tool_timeout_without_runtime_cap(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _ToolNativeTimeoutErrorTool(), tool_timeout_seconds=None)

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id="tool-native-timeout")))

    replay = runtime.resume("tool-native-timeout")
    event_types = [event.event_type for event in replay.events]
    completed_events = [event for event in replay.events if event.event_type == "runtime.tool_completed"]

    assert "runtime.tool_timeout" not in event_types
    assert len(completed_events) == 1
    assert completed_events[0].payload["status"] == "error"
    assert completed_events[0].payload["error"] == "tool-native timeout"


def test_tool_native_timeout_error_before_runtime_cap_does_not_emit_runtime_tool_timeout(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _ToolNativeTimeoutErrorTool(), tool_timeout_seconds=10)

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id="tool-native-timeout-with-cap")))

    replay = runtime.resume("tool-native-timeout-with-cap")
    event_types = [event.event_type for event in replay.events]
    completed_events = [event for event in replay.events if event.event_type == "runtime.tool_completed"]

    assert "runtime.tool_timeout" not in event_types
    assert len(completed_events) == 1
    assert completed_events[0].payload["status"] == "error"
    assert completed_events[0].payload["error"] == "tool-native timeout"


def test_tool_started_event_includes_display_and_tool_status_metadata(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _InstantTool(), tool_timeout_seconds=None)

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    started_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_started"
    ]

    assert len(started_events) >= 1, "expected at least one runtime.tool_started event"
    payload = started_events[0].payload

    assert "display" in payload
    assert "tool_status" in payload
    assert "tool" in payload


def test_tool_completed_event_includes_tool_status_metadata(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _InstantTool(), tool_timeout_seconds=None)

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) >= 1, "expected at least one runtime.tool_completed event"
    payload = completed_events[0].payload

    assert "display" in payload
    assert "tool_status" in payload
    assert "tool" in payload

    display_value = payload["display"]
    assert isinstance(display_value, dict)
    assert display_value["kind"] == "generic"
    assert display_value["title"] == "instant_tool"
    assert display_value["summary"] == "instant_tool"

    tool_status_value = payload["tool_status"]
    assert isinstance(tool_status_value, dict)
    typed_ts = cast(dict[str, object], tool_status_value)
    nested_display = typed_ts.get("display")
    assert isinstance(nested_display, dict)


def test_tool_completed_payload_carries_active_model_and_provider(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([_InstantTool()]),
        graph=_SingleToolCallGraph("instant_tool"),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            execution_engine="deterministic",
            model="opencode-go/glm-5.1",
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) >= 1
    payload = completed_events[0].payload

    assert payload["model"] == "opencode-go/glm-5.1"
    assert payload["provider"] == "opencode-go"
    # Additive metadata: existing identity keys are preserved.
    assert payload["tool"] == "instant_tool"
    assert payload["status"] == "ok"


def test_tool_completed_payload_omits_model_without_model_metadata(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _InstantTool(), tool_timeout_seconds=None)

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) >= 1
    payload = completed_events[0].payload

    assert "model" not in payload
    assert "provider" not in payload


def test_timeout_exit_emits_terminal_tool_status_with_error(
    tmp_path: Path,
) -> None:
    """Runtime timeout path emits a terminal runtime.tool_completed with error status."""
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph(
            {
                "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                "timeout": 10,
            }
        ),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.tool_completed"]

    assert len(completed_events) == 1, "expected one runtime.tool_completed on timeout exit"
    payload = completed_events[0].payload

    assert payload["status"] == "error", "terminal tool status must be error"
    assert payload["tool"] == "shell_exec"
    assert payload["diagnostics"]["kind"] == "tool_timeout"
    assert payload["diagnostics"]["summary"] == "tool 'shell_exec' exceeded runtime timeout of 1s"
    # The details are additive since the timeout records its execution facts:
    # shell_exec kills its own process on the runtime timeout, so cancellation
    # was not signalled by the runtime and the execution is confirmed stopped.
    assert payload["diagnostics"]["details"] == {
        "tool_name": "shell_exec",
        "timed_out": True,
        "timeout_seconds": 1,
        "cancellation_signalled": False,
        "execution_stopped": True,
        "side_effect_state": "settled",
    }
    assert payload["diagnostics"]["guidance"] == "Reduce the command scope, increase the timeout, or retry."

    assert "tool_call_id" in payload
    assert isinstance(payload["tool_call_id"], str)

    assert "display" in payload, "terminal status must include display metadata"
    assert "tool_status" in payload, "terminal status must include tool_status"

    tool_status = cast(dict[str, object], payload["tool_status"])
    assert tool_status["phase"] == "failed"
    assert tool_status["status"] == "failed"
    assert tool_status["tool_name"] == "shell_exec"

    # Verify ordering: started before terminal completed
    event_types = [c.event.event_type for c in chunks if c.kind == "event" and c.event is not None]
    started_idx = event_types.index("runtime.tool_started")
    completed_idx = event_types.index("runtime.tool_completed")
    assert started_idx < completed_idx, "runtime.tool_completed must follow runtime.tool_started"

    # Verify tool_call_id matches the started event (frontend row identity)
    started_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.tool_started"]
    assert len(started_events) >= 1
    started_call_id = started_events[0].payload["tool_call_id"]
    assert isinstance(started_call_id, str)
    assert payload["tool_call_id"] == started_call_id, "terminal tool_completed must use same tool_call_id as tool_started"


def test_unrecovered_exception_emits_terminal_tool_status_before_failure(
    tmp_path: Path,
) -> None:
    """Unrecovered tool exception emits terminal runtime.tool_completed before runtime.failed."""
    tool = _FatalExceptionTool()
    runtime = _make_runtime(tmp_path, tool, tool_timeout_seconds=None)

    chunks: list[Any] = []
    try:
        for chunk in runtime.run_stream(RuntimeRequest(prompt="go")):
            chunks.append(chunk)
    except ValueError:
        pass

    completed_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.tool_completed"]
    failed_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.failed"]

    assert len(completed_events) == 1, "expected one runtime.tool_completed before runtime.failed on unrecovered exception"
    payload = completed_events[0].payload

    assert payload["status"] == "error", "terminal tool status must be error"
    assert payload["tool"] == "fatal_exception_tool"
    assert payload["error"] == "fatal tool error"

    assert "tool_call_id" in payload
    assert isinstance(payload["tool_call_id"], str)

    assert "display" in payload, "terminal status must include display metadata"
    assert "tool_status" in payload, "terminal status must include tool_status"

    tool_status = cast(dict[str, object], payload["tool_status"])
    assert tool_status["phase"] == "failed"
    assert tool_status["status"] == "failed"
    assert tool_status["tool_name"] == "fatal_exception_tool"

    # runtime.failed must also be present (existing contract preserved)
    assert len(failed_events) >= 1, "runtime.failed must still be emitted"

    # Verify ordering: started → completed → failed
    event_types = [c.event.event_type for c in chunks if c.kind == "event" and c.event is not None]
    started_idx = event_types.index("runtime.tool_started")
    completed_idx = event_types.index("runtime.tool_completed")
    failed_idx = event_types.index("runtime.failed")
    assert started_idx < completed_idx < failed_idx, "events must be ordered: started → completed → failed"

    # Verify tool_call_id matches the started event (frontend row identity)
    started_events = [c.event for c in chunks if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.tool_started"]
    assert len(started_events) >= 1
    started_call_id = started_events[0].payload["tool_call_id"]
    assert isinstance(started_call_id, str)
    assert payload["tool_call_id"] == started_call_id, "terminal tool_completed must use same tool_call_id as tool_started"


def test_timeout_replay_preserves_terminal_tool_status_with_matching_call_id(
    tmp_path: Path,
) -> None:
    """Replay after timeout includes terminal runtime.tool_completed with matched tool_call_id."""
    session_id = "timeout-replay-terminal-call-id"
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph(
            {
                "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
                "timeout": 10,
            }
        ),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))
    replay = runtime.resume(session_id)
    replay_events = replay.events

    completed_events = [e for e in replay_events if e.event_type == "runtime.tool_completed"]
    started_events = [e for e in replay_events if e.event_type == "runtime.tool_started"]

    assert len(completed_events) == 1, "replay must contain one terminal runtime.tool_completed"
    completed_payload = completed_events[0].payload
    assert completed_payload["status"] == "error"
    assert completed_payload["tool"] == "shell_exec"
    assert completed_payload["diagnostics"]["kind"] == "tool_timeout"
    assert completed_payload["diagnostics"]["summary"] == "tool 'shell_exec' exceeded runtime timeout of 1s"

    started_call_id = started_events[0].payload["tool_call_id"]
    assert isinstance(started_call_id, str)
    completed_call_id = completed_payload["tool_call_id"]
    assert isinstance(completed_call_id, str)
    assert started_call_id == completed_call_id, "replay must preserve same tool_call_id between tool_started and terminal tool_completed"

    replay_event_types = [e.event_type for e in replay_events]
    assert "runtime.tool_timeout" in replay_event_types
    assert "runtime.failed" in replay_event_types


class _ArtifactThenUriReadGraph:
    """Runs the large-output tool, then reads the spilled artifact via URI."""

    def __init__(self) -> None:
        self.artifact_id: str | None = None

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> Any:
        _ = request, session

        class _Step:
            reasoning: str | None = None

        step = _Step()
        if not tool_results:
            step.tool_call = ToolCall(tool_name="large_output_tool", arguments={})  # type: ignore[attr-defined]
            step.output = None  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = False  # type: ignore[attr-defined]
            return step
        if self.artifact_id is None:
            first_result = tool_results[0]
            self.artifact_id = str(first_result.data.get("artifact_id") or "")
            assert self.artifact_id, "large output tool result must carry an artifact_id"
            step.tool_call = ToolCall(  # type: ignore[attr-defined]
                tool_name="read",
                arguments={"path": f"voidcode://artifact/{self.artifact_id}", "limit": 100},
            )
            step.output = None  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = False  # type: ignore[attr-defined]
            return step
        step.tool_call = None  # type: ignore[attr-defined]
        step.output = "completed"  # type: ignore[attr-defined]
        step.events = ()  # type: ignore[attr-defined]
        step.is_finished = True  # type: ignore[attr-defined]
        return step


def test_read_artifact_uri_reads_own_session_artifact_end_to_end(tmp_path: Path) -> None:
    """The URI resolves a real spilled artifact for the owning session."""
    session_id = "artifact-uri-owner"
    graph = _ArtifactThenUriReadGraph()
    registry = ToolRegistry.from_tools([_LargeOutputTool(), ReadTool()])
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=registry,
        graph=graph,
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))
    completed_events = [
        chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None and chunk.event.event_type == "runtime.tool_completed"
    ]
    read_events = [event for event in completed_events if event.payload.get("tool") == "read"]
    assert len(read_events) == 1, "expected one read completion for the artifact URI"
    payload = read_events[0].payload
    assert payload["status"] == "ok"
    assert payload["type"] == "artifact"
    assert payload["artifact_id"] == graph.artifact_id
    assert payload["raw_content"] == "".join(f"line-{index}\n" for index in range(100))
    assert payload["next_offset"] == 100
    assert payload["line_count"] == 2100
    assert payload["truncated"] is True
    assert payload["partial"] is True


class _ForeignArtifactUriReadGraph:
    """Immediately issues a read URI for a pre-seeded foreign artifact id."""

    def __init__(self, artifact_id: str) -> None:
        self._artifact_id = artifact_id
        self._done = False

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> Any:
        _ = request, tool_results, session

        class _Step:
            reasoning: str | None = None

        step = _Step()
        if not self._done:
            step.tool_call = ToolCall(  # type: ignore[attr-defined]
                tool_name="read",
                arguments={"path": f"voidcode://artifact/{self._artifact_id}", "limit": 100},
            )
            step.output = None  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = False  # type: ignore[attr-defined]
            self._done = True
        else:
            step.tool_call = None  # type: ignore[attr-defined]
            step.output = "completed"  # type: ignore[attr-defined]
            step.events = ()  # type: ignore[attr-defined]
            step.is_finished = True  # type: ignore[attr-defined]
        return step


def test_read_artifact_uri_rejects_foreign_session_artifact(tmp_path: Path) -> None:
    """An artifact created in session A is not resolvable from session B."""
    owner_session = "artifact-uri-owner-b"
    foreign_session = "artifact-uri-foreign-b"
    owner_graph = _ArtifactThenUriReadGraph()
    owner_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([_LargeOutputTool(), ReadTool()]),
        graph=owner_graph,
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
        ),
    )
    _ = list(owner_runtime.run_stream(RuntimeRequest(prompt="go", session_id=owner_session)))
    assert owner_graph.artifact_id

    foreign_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool()]),
        graph=_ForeignArtifactUriReadGraph(owner_graph.artifact_id),
        config=RuntimeConfig(
            mcp=RuntimeMcpConfig(enabled=False),
            approval_mode="allow",
            execution_engine="deterministic",
        ),
    )
    with pytest.raises(ValueError, match="artifact not found in current session"):
        _ = list(foreign_runtime.run_stream(RuntimeRequest(prompt="go", session_id=foreign_session)))
