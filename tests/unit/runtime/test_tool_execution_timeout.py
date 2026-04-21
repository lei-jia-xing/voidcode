from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.tools import ShellExecTool
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult


class _InstantTool:
    definition = ToolDefinition(name="instant_tool", description="Returns instantly.")

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        return ToolResult(tool_name=self.definition.name, status="ok", content="done")


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
    config = RuntimeConfig(tool_timeout_seconds=tool_timeout_seconds)
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
        config=RuntimeConfig(approval_mode="allow", tool_timeout_seconds=timeout),
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
        config=RuntimeConfig(approval_mode="allow", tool_timeout_seconds=1),
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
        config=RuntimeConfig(approval_mode="allow", tool_timeout_seconds=1),
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
        config=RuntimeConfig(approval_mode="allow", tool_timeout_seconds=None),
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
    assert completed_events[0].payload["timeout"] == 30


def test_shell_exec_timeout_wins_when_shorter_than_runtime_timeout(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(2)"'
    session_id = "shell-exec-local-timeout"
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph({"command": command, "timeout": 1}),
        config=RuntimeConfig(approval_mode="allow", tool_timeout_seconds=10),
    )

    with pytest.raises(ValueError, match="shell_exec command timed out after 1s"):
        _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=session_id)))

    replay = runtime.resume(session_id)
    event_types = [event.event_type for event in replay.events]

    assert "runtime.tool_timeout" not in event_types
    assert replay.events[-1].event_type == "runtime.failed"
    assert replay.events[-1].payload == {"error": "shell_exec command timed out after 1s"}


def test_runtime_timeout_wins_when_shorter_than_shell_exec_timeout(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(2)"'
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ShellExecTool()]),
        graph=_ShellExecGraph({"command": command, "timeout": 10}),
        config=RuntimeConfig(approval_mode="allow", tool_timeout_seconds=1),
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
        config=RuntimeConfig(approval_mode="allow", tool_timeout_seconds=1),
    )

    _ = list(runtime.run_stream(RuntimeRequest(prompt="go")))
    time.sleep(1.5)

    assert side_effect_path.exists() is False


def test_tool_native_timeout_error_does_not_emit_runtime_tool_timeout_without_runtime_cap(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _ToolNativeTimeoutErrorTool(), tool_timeout_seconds=None)

    with pytest.raises(TimeoutError, match="tool-native timeout"):
        _ = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id="tool-native-timeout")))

    replay = runtime.resume("tool-native-timeout")
    event_types = [event.event_type for event in replay.events]

    assert "runtime.tool_timeout" not in event_types
    assert replay.events[-1].event_type == "runtime.failed"
    assert replay.events[-1].payload == {"error": "tool-native timeout"}


def test_tool_native_timeout_error_before_runtime_cap_does_not_emit_runtime_tool_timeout(
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, _ToolNativeTimeoutErrorTool(), tool_timeout_seconds=10)

    with pytest.raises(TimeoutError, match="tool-native timeout"):
        _ = list(
            runtime.run_stream(
                RuntimeRequest(prompt="go", session_id="tool-native-timeout-with-cap")
            )
        )

    replay = runtime.resume("tool-native-timeout-with-cap")
    event_types = [event.event_type for event in replay.events]

    assert "runtime.tool_timeout" not in event_types
    assert replay.events[-1].event_type == "runtime.failed"
    assert replay.events[-1].payload == {"error": "tool-native timeout"}
