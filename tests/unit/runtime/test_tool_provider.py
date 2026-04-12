from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.mcp import (
    McpConfigState,
    McpManagerState,
    McpToolCallResult,
    McpToolDescriptor,
)
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import (
    GraphRunRequest,
    RuntimeRequest,
    SessionState,
    ToolRegistry,
    VoidCodeRuntime,
)
from voidcode.runtime.tool_provider import BuiltinToolProvider
from voidcode.tools import (
    CodeSearchTool,
    EditTool,
    GlobTool,
    GrepTool,
    ListTool,
    McpTool,
    MultiEditTool,
    ReadFileTool,
    ShellExecTool,
    TodoWriteTool,
    ToolCall,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult


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


class _StubMcpGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = session
        if not tool_results:
            return _StubStep(
                tool_call=ToolCall(
                    tool_name="mcp/echo/echo",
                    arguments={"message": "hi"},
                )
            )
        return _StubStep(output=request.prompt, is_finished=True)


class _InjectedToolGraph:
    def __init__(self) -> None:
        self._step_count = 0

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[object, ...],
        *,
        session: SessionState,
    ) -> _StubStep:
        _ = request, tool_results, session
        self._step_count += 1
        if self._step_count == 1:
            return _StubStep(
                tool_call=ToolCall(tool_name="mcp/echo/echo", arguments={"message": "hi"})
            )
        if self._step_count == 2:
            return _StubStep(tool_call=ToolCall(tool_name="injected_tool", arguments={}))
        return _StubStep(output="done", is_finished=True)


class _InjectedTool:
    definition = ToolDefinition(
        name="injected_tool",
        description="Injected test tool",
        input_schema={"type": "object"},
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        return ToolResult(
            tool_name="injected_tool",
            status="ok",
            content="injected ok",
        )


def test_builtin_tool_provider_returns_expected_builtin_tools() -> None:
    tools = BuiltinToolProvider().provide_tools()

    expected_tools = (
        EditTool,
        GlobTool,
        GrepTool,
        ListTool,
        ReadFileTool,
        ShellExecTool,
        WebFetchTool,
        WebSearchTool,
        WriteFileTool,
        # Optional tools may be present
        CodeSearchTool,
        MultiEditTool,
        TodoWriteTool,
    )

    tool_types = tuple(type(tool) for tool in tools)
    for expected in expected_tools:
        assert expected in tool_types, f"Missing tool: {expected.__name__}"


def test_builtin_tool_provider_can_include_runtime_managed_mcp_tools() -> None:
    def _requester(
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpToolCallResult:
        _ = server_name, tool_name, arguments, workspace
        return McpToolCallResult(content=[{"type": "text", "text": "ok"}], is_error=False)

    mcp_tool = McpTool(
        server_name="echo",
        tool_name="echo",
        description="Echo input",
        input_schema={"type": "object"},
        requester=_requester,
    )

    tools = BuiltinToolProvider(mcp_tools=(mcp_tool,)).provide_tools()

    assert any(tool.definition.name == "mcp/echo/echo" for tool in tools)


def test_tool_registry_accepts_tools_from_provider_output() -> None:
    registry = ToolRegistry.from_tools(BuiltinToolProvider().provide_tools())

    core_tools = {
        "edit",
        "glob",
        "grep",
        "list",
        "read_file",
        "shell_exec",
        "web_fetch",
        "web_search",
        "write_file",
    }
    for tool_name in core_tools:
        assert tool_name in registry.tools, f"Missing core tool: {tool_name}"
        assert registry.resolve(tool_name).definition.name == tool_name

    # Optional tools
    optional_tools = {"apply_patch", "code_search", "multi_edit", "todo_write"}
    for tool_name in optional_tools:
        if tool_name in registry.tools:
            assert registry.resolve(tool_name).definition.name == tool_name


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

    core_tools = [
        "edit",
        "glob",
        "grep",
        "list",
        "read_file",
        "shell_exec",
        "web_fetch",
        "web_search",
        "write_file",
    ]
    for i, tool_name in enumerate(core_tools):
        assert tool_name in registry.tools, f"Missing core tool: {tool_name}"
        assert registry.resolve(tool_name) is provided_tools[i]

    # Verify optional tools if present
    optional_tools = ["apply_patch", "code_search", "multi_edit", "todo_write"]
    for tool_name in optional_tools:
        if tool_name in registry.tools:
            assert registry.resolve(tool_name) is not None


def test_runtime_registry_includes_discovered_mcp_tools(tmp_path: Path) -> None:
    class _StubMcpManager:
        def __init__(self) -> None:
            self.list_tools_calls = 0

        @property
        def configuration(self) -> McpConfigState:
            return McpConfigState(configured_enabled=True)

        def current_state(self) -> McpManagerState:
            return McpManagerState(mode="managed", configuration=self.configuration)

        def list_tools(self, *, workspace: Path):
            _ = workspace
            self.list_tools_calls += 1
            return (
                McpToolDescriptor(
                    server_name="echo",
                    tool_name="echo",
                    description="Echo input",
                    input_schema={"type": "object"},
                ),
            )

        def call_tool(
            self,
            *,
            server_name: str,
            tool_name: str,
            arguments: dict[str, object],
            workspace: Path,
        ) -> McpToolCallResult:
            _ = server_name, tool_name, arguments, workspace
            return McpToolCallResult(content=[{"type": "text", "text": "echo:hi"}])

        def shutdown(self):
            return ()

        def drain_events(self):
            return ()

    mcp_manager = _StubMcpManager()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_StubMcpGraph(),
        mcp_manager=mcp_manager,
        permission_policy=PermissionPolicy(mode="allow"),
    )
    assert mcp_manager.list_tools_calls == 0

    response = runtime.run(RuntimeRequest(prompt="done"))

    assert response.output == "done"
    assert mcp_manager.list_tools_calls == 1
    assert any(
        event.event_type == "runtime.tool_lookup_succeeded"
        and event.payload == {"tool": "mcp/echo/echo"}
        for event in response.events
    )
    assert any(
        event.event_type == "runtime.tool_completed"
        and event.payload["server"] == "echo"
        and event.payload["tool"] == "echo"
        for event in response.events
    )


def test_runtime_refresh_preserves_injected_tool_registry_entries(tmp_path: Path) -> None:
    class _StubMcpManager:
        @property
        def configuration(self) -> McpConfigState:
            return McpConfigState(configured_enabled=True)

        def current_state(self) -> McpManagerState:
            return McpManagerState(mode="managed", configuration=self.configuration)

        def list_tools(self, *, workspace: Path):
            _ = workspace
            return (
                McpToolDescriptor(
                    server_name="echo",
                    tool_name="echo",
                    description="Echo input",
                    input_schema={"type": "object"},
                ),
            )

        def call_tool(
            self,
            *,
            server_name: str,
            tool_name: str,
            arguments: dict[str, object],
            workspace: Path,
        ) -> McpToolCallResult:
            _ = server_name, tool_name, arguments, workspace
            return McpToolCallResult(content=[{"type": "text", "text": "echo:hi"}])

        def shutdown(self):
            return ()

        def drain_events(self):
            return ()

    injected_tool = _InjectedTool()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_InjectedToolGraph(),
        tool_registry=ToolRegistry.from_tools((injected_tool,)),
        mcp_manager=_StubMcpManager(),
        permission_policy=PermissionPolicy(mode="allow"),
    )

    response = runtime.run(RuntimeRequest(prompt="go"))

    assert response.output == "done"
    assert any(
        event.event_type == "runtime.tool_lookup_succeeded"
        and event.payload == {"tool": "injected_tool"}
        for event in response.events
    )


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


def test_tools_package_exports_code_search_tool() -> None:
    tools_module = __import__("voidcode.tools", fromlist=["__all__"])
    assert "CodeSearchTool" in tools_module.__all__
    assert "McpTool" in tools_module.__all__
