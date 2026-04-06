from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from voidcode.runtime.events import EventEnvelope

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.service import (
    GraphRunRequest,
    RuntimeRequest,
    SessionState,
    ToolRegistry,
    VoidCodeRuntime,
)
from voidcode.runtime.tool_provider import BuiltinToolProvider
from voidcode.tools import GrepTool, ReadFileTool, ShellExecTool, ToolCall, WriteFileTool


@dataclass(slots=True)
class _StubStep:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[EventEnvelope, ...] = ()
    is_finished: bool = False


class _StubGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        if not tool_results:
            return _StubStep(
                tool_call=ToolCall(
                    tool_name="grep", arguments={"pattern": "alpha", "path": "sample.txt"}
                )
            )
        return _StubStep(output=request.prompt, is_finished=True)


def test_builtin_tool_provider_returns_expected_builtin_tools() -> None:
    tools = BuiltinToolProvider().provide_tools()

    assert tuple(type(tool) for tool in tools) == (
        GrepTool,
        ReadFileTool,
        ShellExecTool,
        WriteFileTool,
    )


def test_tool_registry_accepts_tools_from_provider_output() -> None:
    registry = ToolRegistry.from_tools(BuiltinToolProvider().provide_tools())

    assert tuple(registry.tools) == ("grep", "read_file", "shell_exec", "write_file")
    assert registry.resolve("grep").definition.name == "grep"
    assert registry.resolve("read_file").definition.name == "read_file"
    assert registry.resolve("shell_exec").definition.name == "shell_exec"
    assert registry.resolve("write_file").definition.name == "write_file"


def test_tool_registry_with_defaults_delegates_through_builtin_provider() -> None:
    provided_tools = BuiltinToolProvider().provide_tools()

    with patch.object(
        BuiltinToolProvider,
        "provide_tools",
        autospec=True,
        return_value=provided_tools,
    ) as provide_tools_mock:
        registry = ToolRegistry.with_defaults()

    provide_tools_mock.assert_called_once()
    provider = provide_tools_mock.call_args.args[0]
    assert isinstance(provider, BuiltinToolProvider)
    assert tuple(registry.tools) == ("grep", "read_file", "shell_exec", "write_file")
    assert registry.resolve("grep") is provided_tools[0]
    assert registry.resolve("read_file") is provided_tools[1]
    assert registry.resolve("shell_exec") is provided_tools[2]
    assert registry.resolve("write_file") is provided_tools[3]


def test_runtime_default_registry_behavior_remains_unchanged(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha beta\n", encoding="utf-8")
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_StubGraph())
    response = runtime.run(RuntimeRequest(prompt="hello"))

    assert response.output == "hello"
    assert response.events[1].event_type == "runtime.skills_loaded"
    assert response.events[1].payload == {"skills": []}
    assert response.events[3].event_type == "runtime.tool_lookup_succeeded"
    assert response.events[3].payload == {"tool": "grep"}
    assert response.events[5].event_type == "runtime.tool_completed"
    assert response.events[5].payload["pattern"] == "alpha"
