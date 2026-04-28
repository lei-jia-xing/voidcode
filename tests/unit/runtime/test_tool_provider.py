from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from voidcode.graph.contracts import GraphEvent, GraphStep
from voidcode.hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
from voidcode.mcp import McpToolSafety
from voidcode.runtime.config import RuntimeConfig
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
    AstGrepPreviewTool,
    AstGrepReplaceTool,
    AstGrepSearchTool,
    BackgroundCancelTool,
    BackgroundOutputTool,
    CodeSearchTool,
    EditTool,
    GlobTool,
    GrepTool,
    ListTool,
    McpTool,
    MultiEditTool,
    QuestionTool,
    ReadFileTool,
    ShellExecTool,
    SkillTool,
    TaskTool,
    TodoWriteTool,
    ToolCall,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from voidcode.tools.contracts import ToolDefinition, ToolResult
from voidcode.tools.guidance import guidance_filename_for_tool, guidance_for_tool

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


@dataclass(slots=True)
class _StubStep:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[GraphEvent, ...] = ()
    is_finished: bool = False


class _StubGraph:
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep:
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
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep:
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
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep:
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


class _FormatTestTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="format_file",
            description="Format a file for tests",
            input_schema={"type": "object"},
            read_only=False,
        )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        return ToolResult(tool_name="format_file", status="ok", content="formatted")


def test_builtin_tool_provider_returns_expected_builtin_tools() -> None:
    tools = BuiltinToolProvider().provide_tools()

    expected_tools = (
        EditTool,
        GlobTool,
        GrepTool,
        ListTool,
        ReadFileTool,
        ShellExecTool,
        QuestionTool,
        SkillTool,
        AstGrepSearchTool,
        AstGrepPreviewTool,
        AstGrepReplaceTool,
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


def test_builtin_tool_provider_can_include_runtime_managed_format_tool() -> None:
    format_tool = _FormatTestTool()

    tools = BuiltinToolProvider(format_tool=format_tool).provide_tools()

    assert any(tool.definition.name == "format_file" for tool in tools)


def test_builtin_tool_provider_injects_formatter_aware_edit_tools(tmp_path: Path) -> None:
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("VALUE='BETA'\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    target = tmp_path / "sample.py"
    target.write_text("value='alpha'\n", encoding="utf-8")

    tools = BuiltinToolProvider(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            }
        )
    ).provide_tools()

    edit_tool = next(tool for tool in tools if tool.definition.name == "edit")
    result = edit_tool.invoke(
        ToolCall(
            tool_name="edit",
            arguments={"path": "sample.py", "oldString": "'alpha'", "newString": "'beta'"},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "VALUE='BETA'\n"
    formatter_payload = cast(dict[str, object], result.data["formatter"])
    assert formatter_payload["status"] == "formatted"


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
    optional_tools = {
        "apply_patch",
        "ast_grep_search",
        "ast_grep_preview",
        "ast_grep_replace",
        "code_search",
        "multi_edit",
        "todo_write",
    }
    for tool_name in optional_tools:
        if tool_name in registry.tools:
            assert registry.resolve(tool_name).definition.name == tool_name


def test_default_runtime_scopes_tools_to_leader_manifest(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(execution_engine="provider", model="opencode-go/glm-5.1"),
        graph=_StubGraph(),
    )
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    response = runtime.run(RuntimeRequest(prompt="go"))
    runtime_config = response.session.metadata["runtime_config"]
    assert isinstance(runtime_config, dict)
    agent = cast(dict[str, object], runtime_config["agent"])
    assert isinstance(agent, dict)

    assert agent["preset"] == "leader"
    assert any(event.event_type == "runtime.tool_lookup_succeeded" for event in response.events)
    assert all(event.payload.get("tool") != "missing_tool" for event in response.events)


def test_builtin_tool_definitions_include_sidecar_guidance() -> None:
    registry = ToolRegistry.from_tools(BuiltinToolProvider().provide_tools())
    definitions = {definition.name: definition for definition in registry.definitions()}

    assert definitions["write_file"].description.startswith("Writes a file to the local workspace.")
    assert (
        "new file or intentionally replacing the whole file"
        in definitions["write_file"].description
    )
    assert definitions["ast_grep_search"].description.startswith(
        "Use ast-grep tools for structural code matching"
    )
    assert (
        "ast_grep_replace applies a structural rewrite"
        in definitions["ast_grep_search"].description
    )
    assert definitions["web_fetch"].description.startswith("- Fetches content from a specified URL")
    assert "http or https URL" in definitions["web_fetch"].description


def test_sidecar_guidance_mapping_covers_builtin_runtime_tool_names() -> None:
    runtime_tool_names = {
        "apply_patch",
        "ast_grep_preview",
        "ast_grep_replace",
        "ast_grep_search",
        "background_cancel",
        "background_output",
        "code_search",
        "edit",
        "format_file",
        "glob",
        "grep",
        "list",
        "lsp",
        "multi_edit",
        "question",
        "read_file",
        "shell_exec",
        "skill",
        "task",
        "todo_write",
        "web_fetch",
        "web_search",
        "write_file",
    }

    for tool_name in runtime_tool_names:
        filename = guidance_filename_for_tool(tool_name)
        assert filename is not None, f"Missing guidance mapping for {tool_name}"
        assert guidance_for_tool(tool_name), f"Missing sidecar content for {tool_name}"


def test_dynamic_mcp_tool_definitions_include_shared_policy_guidance() -> None:
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
    registry = ToolRegistry.from_tools((mcp_tool,))

    definition = registry.definitions()[0]

    assert definition.name == "mcp/echo/echo"
    assert "Agent usage guidance:" in definition.description
    assert "Dynamic MCP tools are runtime-discovered" in definition.description


def test_dynamic_mcp_tool_definitions_apply_server_safety_hints() -> None:
    def _requester(
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpToolCallResult:
        _ = server_name, tool_name, arguments, workspace
        return McpToolCallResult(content=[{"type": "text", "text": "ok"}], is_error=False)

    read_only_tool = McpTool(
        server_name="echo",
        tool_name="inspect",
        description="Inspect input",
        input_schema={"type": "object"},
        safety=McpToolSafety.from_hints(read_only_hint=True, destructive_hint=False),
        requester=_requester,
    )
    mutating_tool = McpTool(
        server_name="echo",
        tool_name="write",
        description="Write input",
        input_schema={"type": "object"},
        safety=McpToolSafety.from_hints(read_only_hint=True, destructive_hint=True),
        requester=_requester,
    )

    assert read_only_tool.definition.read_only is True
    assert mutating_tool.definition.read_only is False


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
    optional_tools = [
        "apply_patch",
        "ast_grep_search",
        "ast_grep_preview",
        "ast_grep_replace",
        "code_search",
        "multi_edit",
        "todo_write",
    ]
    for tool_name in optional_tools:
        if tool_name in registry.tools:
            assert registry.resolve(tool_name) is not None


def test_tool_registry_with_defaults_passes_format_tool_to_builtin_provider() -> None:
    format_tool = _FormatTestTool()
    provided_tools = (format_tool,)

    with patch.object(
        BuiltinToolProvider,
        "provide_tools",
        autospec=True,
        return_value=provided_tools,
    ) as provide_tools_mock:
        registry = ToolRegistry.with_defaults(format_tool=format_tool)

    provide_tools_mock.assert_called_once()
    provider = provide_tools_mock.call_args.args[0]
    assert isinstance(provider, BuiltinToolProvider)
    assert registry.resolve("format_file") is format_tool


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

        def retry_connections(self, *, workspace: Path) -> None:
            _ = workspace
            return None

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

        def retry_connections(self, *, workspace: Path) -> None:
            _ = workspace
            return None

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
    base_registry = runtime._base_tool_registry  # pyright: ignore[reportPrivateUsage]

    assert "format_file" in base_registry.tools

    response = runtime.run(RuntimeRequest(prompt="hello"))

    assert response.output == "hello"
    assert response.events[1].event_type == "runtime.skills_loaded"
    assert response.events[1].payload["skills"] == []
    assert response.events[1].payload["selected_skills"] == []
    assert response.events[1].payload["catalog_context_length"] == 0
    assert response.events[3].event_type == "runtime.tool_lookup_succeeded"
    assert response.events[3].payload == {"tool": "grep"}
    assert response.events[4].event_type == "runtime.permission_resolved"
    assert response.events[5].event_type == "runtime.tool_started"
    assert response.events[6].event_type == "runtime.tool_completed"
    assert response.events[6].payload["pattern"] == "alpha"


def test_runtime_default_registry_includes_runtime_backed_agent_tools(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(workspace=tmp_path, graph=_StubGraph())
    base_registry = runtime._base_tool_registry  # pyright: ignore[reportPrivateUsage]

    assert isinstance(base_registry.resolve("skill"), SkillTool)
    assert isinstance(base_registry.resolve("task"), TaskTool)
    assert isinstance(base_registry.resolve("question"), QuestionTool)
    assert isinstance(base_registry.resolve("background_output"), BackgroundOutputTool)
    assert isinstance(base_registry.resolve("background_cancel"), BackgroundCancelTool)


def test_tools_package_exports_code_search_tool() -> None:
    tools_module = __import__("voidcode.tools", fromlist=["__all__"])
    assert "AstGrepSearchTool" in tools_module.__all__
    assert "AstGrepPreviewTool" in tools_module.__all__
    assert "AstGrepReplaceTool" in tools_module.__all__
    assert "CodeSearchTool" in tools_module.__all__
    assert "McpTool" in tools_module.__all__
