from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.contracts import (
    RuntimeProviderContextSegmentSnapshot,
    RuntimeRequest,
    RuntimeResponse,
)
from voidcode.runtime.events import RUNTIME_TOOL_PROGRESS, EventEnvelope
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.tools import ShellExecTool, tool_output_artifact_temp_root
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult
from voidcode.tools.runtime_context import (
    RuntimeToolInvocationContext,
    bind_runtime_tool_context,
)


class _AbortSignal:
    def __init__(self, *, cancelled: bool = False, reason: str | None = None) -> None:
        self._cancelled = cancelled
        self.reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled


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


class _SlowButFinishingTool:
    definition = ToolDefinition(
        name="slow_but_finishing_tool", description="Finishes after a short sleep."
    )

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
            pass

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
            pass

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
    event_types = [
        chunk.event.event_type
        for chunk in first_chunks
        if chunk.kind == "event" and chunk.event is not None
    ]

    assert elapsed < 5
    assert "runtime.tool_timeout" not in event_types
    assert "runtime.failed" not in event_types


def test_hanging_tool_does_not_emit_tool_completed(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, _HangingTool(), tool_timeout_seconds=1)

    iterator = runtime.run_stream(RuntimeRequest(prompt="go"))
    first_chunks = [next(iterator) for _ in range(3)]
    event_types = [
        chunk.event.event_type
        for chunk in first_chunks
        if chunk.kind == "event" and chunk.event is not None
    ]

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
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=timeout,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    timeout_events = [
        c.event
        for c in chunks
        if c.kind == "event"
        and c.event is not None
        and c.event.event_type == "runtime.tool_timeout"
    ]

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
        config=RuntimeConfig(approval_mode="allow", execution_engine="deterministic"),
    )

    chunks: list[Any] = []
    iterator = runtime.run_stream(RuntimeRequest(prompt="go"))
    first_progress = None
    for chunk in iterator:
        chunks.append(chunk)
        if (
            chunk.kind == "event"
            and chunk.event is not None
            and chunk.event.event_type == RUNTIME_TOOL_PROGRESS
        ):
            first_progress = chunk.event
            break

    assert first_progress is not None
    assert marker.exists() is False
    assert first_progress.payload["tool"] == "shell_exec"
    assert first_progress.payload["stream"] == "stdout"
    assert first_progress.payload["chunk"] == "alpha\n"

    chunks.extend(iterator)
    event_types = [c.event.event_type for c in chunks if c.kind == "event" and c.event is not None]
    assert event_types.index("runtime.tool_started") < event_types.index(RUNTIME_TOOL_PROGRESS)
    assert event_types.index(RUNTIME_TOOL_PROGRESS) < event_types.index("runtime.tool_completed")
    completed = next(
        c.event
        for c in chunks
        if c.kind == "event"
        and c.event is not None
        and c.event.event_type == "runtime.tool_completed"
    )
    assert completed.payload["stdout"] == "alpha\nomega\n"


def test_shell_exec_runtime_timeout_preserves_partial_progress_and_final_output(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph(
            {
                "command": (
                    f'"{sys.executable}" -c "import sys, time; '
                    "sys.stdout.write('partial\\n'); sys.stdout.flush(); time.sleep(2)"
                    '"'
                ),
                "timeout": 10,
            }
        ),
        config=RuntimeConfig(
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    progress_events = [
        c.event
        for c in chunks
        if c.kind == "event" and c.event is not None and c.event.event_type == RUNTIME_TOOL_PROGRESS
    ]
    completed = next(
        c.event
        for c in chunks
        if c.kind == "event"
        and c.event is not None
        and c.event.event_type == "runtime.tool_completed"
    )

    assert [event.payload["chunk"] for event in progress_events] == ["partial\n"]
    assert completed.payload["status"] == "error"
    assert completed.payload["stdout"] == "partial\n"
    assert completed.payload["interrupted"] is True
    assert completed.payload["timed_out"] is True


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
        chunk.event
        for chunk in chunks
        if chunk.kind == "event"
        and chunk.event is not None
        and chunk.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) == 1
    payload = completed_events[0].payload
    assert payload["truncated"] is True
    assert isinstance(payload["output_path"], str)
    output_path = Path(payload["output_path"]).resolve()
    artifact_root = tool_output_artifact_temp_root().resolve()
    assert artifact_root in output_path.parents
    assert payload["artifact_missing"] is False
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
    completed_event = next(
        event for event in replay.events if event.event_type == "runtime.tool_completed"
    )
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
    completed_event = next(
        event for event in source_replay.events if event.event_type == "runtime.tool_completed"
    )
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
        config=RuntimeConfig(execution_engine="deterministic"),
        session_store=store,
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
            events=(
                EventEnvelope(
                    session_id=session_id,
                    sequence=1,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        "tool": "large_output_tool",
                        "tool_call_id": tool_call_id,
                        "status": "ok",
                        "content": "forged",
                        "artifact": forged_artifact,
                    },
                ),
                EventEnvelope(
                    session_id=session_id,
                    sequence=2,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        **completed_event.payload,
                        "tool_call_id": tool_call_id,
                    },
                ),
            ),
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
        config=RuntimeConfig(execution_engine="deterministic"),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event
        for chunk in chunks
        if chunk.kind == "event"
        and chunk.event is not None
        and chunk.event.event_type == "runtime.tool_completed"
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
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=None,
        ),
    )
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        chunk.event
        for chunk in chunks
        if chunk.kind == "event"
        and chunk.event is not None
        and chunk.event.event_type == "runtime.tool_completed"
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
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=10,
        ),
    )

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))

    replay = runtime.resume(session_id)
    event_types = [event.event_type for event in replay.events]
    completed_events = [
        event for event in replay.events if event.event_type == "runtime.tool_completed"
    ]

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
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    event_types = [
        chunk.event.event_type
        for chunk in chunks
        if chunk.kind == "event" and chunk.event is not None
    ]
    timeout_events = [
        chunk.event
        for chunk in chunks
        if chunk.kind == "event"
        and chunk.event is not None
        and chunk.event.event_type == "runtime.tool_timeout"
    ]

    assert "runtime.failed" in event_types
    assert len(timeout_events) == 1
    assert timeout_events[0].payload == {"tool": "shell_exec", "timeout_seconds": 1}


def test_runtime_timeout_prevents_delayed_shell_exec_side_effect(tmp_path: Path) -> None:
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
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not side_effect_path.exists():
            break
        time.sleep(0.1)

    assert side_effect_path.exists() is False


def test_tool_native_timeout_error_does_not_emit_runtime_tool_timeout_without_runtime_cap(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _ToolNativeTimeoutErrorTool(), tool_timeout_seconds=None)

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id="tool-native-timeout")))

    replay = runtime.resume("tool-native-timeout")
    event_types = [event.event_type for event in replay.events]
    completed_events = [
        event for event in replay.events if event.event_type == "runtime.tool_completed"
    ]

    assert "runtime.tool_timeout" not in event_types
    assert len(completed_events) == 1
    assert completed_events[0].payload["status"] == "error"
    assert completed_events[0].payload["error"] == "tool-native timeout"


def test_tool_native_timeout_error_before_runtime_cap_does_not_emit_runtime_tool_timeout(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _ToolNativeTimeoutErrorTool(), tool_timeout_seconds=10)

    _ = list(
        runtime.run_stream(RuntimeRequest(prompt="go", session_id="tool-native-timeout-with-cap"))
    )

    replay = runtime.resume("tool-native-timeout-with-cap")
    event_types = [event.event_type for event in replay.events]
    completed_events = [
        event for event in replay.events if event.event_type == "runtime.tool_completed"
    ]

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
        chunk.event
        for chunk in chunks
        if chunk.kind == "event"
        and chunk.event is not None
        and chunk.event.event_type == "runtime.tool_started"
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
        chunk.event
        for chunk in chunks
        if chunk.kind == "event"
        and chunk.event is not None
        and chunk.event.event_type == "runtime.tool_completed"
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
            approval_mode="allow",
            execution_engine="deterministic",
            tool_timeout_seconds=1,
        ),
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    completed_events = [
        c.event
        for c in chunks
        if c.kind == "event"
        and c.event is not None
        and c.event.event_type == "runtime.tool_completed"
    ]

    assert len(completed_events) == 1, "expected one runtime.tool_completed on timeout exit"
    payload = completed_events[0].payload

    assert payload["status"] == "error", "terminal tool status must be error"
    assert payload["tool"] == "shell_exec"
    assert payload["error_kind"] == "tool_timeout"
    assert payload["error_summary"] == "tool 'shell_exec' exceeded runtime timeout of 1s"
    assert payload["error_details"] == {
        "tool_name": "shell_exec",
        "message": "tool 'shell_exec' exceeded runtime timeout of 1s",
        "summary": "tool 'shell_exec' exceeded runtime timeout of 1s",
        "error_kind": "tool_timeout",
        "timed_out": True,
        "timeout_seconds": 1,
    }
    assert payload["retry_guidance"] == "Reduce the command scope, increase the timeout, or retry."

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
    started_events = [
        c.event
        for c in chunks
        if c.kind == "event"
        and c.event is not None
        and c.event.event_type == "runtime.tool_started"
    ]
    assert len(started_events) >= 1
    started_call_id = started_events[0].payload["tool_call_id"]
    assert isinstance(started_call_id, str)
    assert payload["tool_call_id"] == started_call_id, (
        "terminal tool_completed must use same tool_call_id as tool_started"
    )


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

    completed_events = [
        c.event
        for c in chunks
        if c.kind == "event"
        and c.event is not None
        and c.event.event_type == "runtime.tool_completed"
    ]
    failed_events = [
        c.event
        for c in chunks
        if c.kind == "event" and c.event is not None and c.event.event_type == "runtime.failed"
    ]

    assert len(completed_events) == 1, (
        "expected one runtime.tool_completed before runtime.failed on unrecovered exception"
    )
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
    assert started_idx < completed_idx < failed_idx, (
        "events must be ordered: started → completed → failed"
    )

    # Verify tool_call_id matches the started event (frontend row identity)
    started_events = [
        c.event
        for c in chunks
        if c.kind == "event"
        and c.event is not None
        and c.event.event_type == "runtime.tool_started"
    ]
    assert len(started_events) >= 1
    started_call_id = started_events[0].payload["tool_call_id"]
    assert isinstance(started_call_id, str)
    assert payload["tool_call_id"] == started_call_id, (
        "terminal tool_completed must use same tool_call_id as tool_started"
    )


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
    assert completed_payload["error_kind"] == "tool_timeout"
    assert completed_payload["error_summary"] == "tool 'shell_exec' exceeded runtime timeout of 1s"

    started_call_id = started_events[0].payload["tool_call_id"]
    assert isinstance(started_call_id, str)
    completed_call_id = completed_payload["tool_call_id"]
    assert isinstance(completed_call_id, str)
    assert started_call_id == completed_call_id, (
        "replay must preserve same tool_call_id between tool_started and terminal tool_completed"
    )

    replay_event_types = [e.event_type for e in replay_events]
    assert "runtime.tool_timeout" in replay_event_types
    assert "runtime.failed" in replay_event_types
