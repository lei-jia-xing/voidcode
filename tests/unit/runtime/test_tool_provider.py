from __future__ import annotations

import json
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
from voidcode.runtime.config import (
    RuntimeAgentConfig,
    RuntimeConfig,
    RuntimeToolsBuiltinConfig,
    RuntimeToolsConfig,
    RuntimeToolsLocalConfig,
)
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
from voidcode.runtime.tool_provider import (
    BuiltinToolProvider,
    LocalCustomToolProvider,
    scoped_tool_registry_for_agent,
)
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
    LocalCustomTool,
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
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context

pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


@pytest.fixture
def _force_deterministic_engine_default(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def _write_local_tool_manifest(
    workspace: Path,
    *,
    name: str = "local/echo",
    read_only: bool = True,
    command: list[str] | None = None,
) -> Path:
    tools_dir = workspace / ".voidcode" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    script = tools_dir / "echo.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys

            args = json.loads(sys.stdin.read() or "{}")
            print(json.dumps({
                "args": args,
                "workspace": os.environ.get("VOIDCODE_WORKSPACE"),
                "session": os.environ.get("VOIDCODE_SESSION_ID"),
            }, sort_keys=True))
            """
        ),
        encoding="utf-8",
    )
    manifest = tools_dir / f"{name.replace('/', '_')}.json"
    manifest.write_text(
        json.dumps(
            {
                "name": name,
                "description": "Echo arguments from a local manifest",
                "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                "command": command or [sys.executable, "${manifest_dir}/echo.py"],
                "read_only": read_only,
            }
        ),
        encoding="utf-8",
    )
    return manifest


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


def test_scoped_tool_registry_returns_original_registry_without_agent() -> None:
    registry = ToolRegistry.from_tools(BuiltinToolProvider().provide_tools())

    scoped = scoped_tool_registry_for_agent(registry, agent=None)

    assert scoped is registry


def test_scoped_tool_registry_applies_manifest_allowlist() -> None:
    registry = ToolRegistry.from_tools(BuiltinToolProvider().provide_tools())

    scoped = scoped_tool_registry_for_agent(registry, agent=RuntimeAgentConfig(preset="explore"))

    assert "read_file" in scoped.tools
    assert "grep" in scoped.tools
    assert "write_file" not in scoped.tools
    assert "task" not in scoped.tools


def test_scoped_tool_registry_can_exclude_builtins() -> None:
    registry = ToolRegistry.from_tools(BuiltinToolProvider().provide_tools())

    scoped = scoped_tool_registry_for_agent(
        registry,
        agent=RuntimeAgentConfig(
            preset="leader",
            tools=RuntimeToolsConfig(builtin=RuntimeToolsBuiltinConfig(enabled=False)),
        ),
    )

    assert scoped.tools == {}


def test_scoped_tool_registry_applies_agent_allowlist_and_default_filters() -> None:
    registry = ToolRegistry.from_tools(BuiltinToolProvider().provide_tools())

    scoped = scoped_tool_registry_for_agent(
        registry,
        agent=RuntimeAgentConfig(
            preset="leader",
            tools=RuntimeToolsConfig(
                allowlist=("read_file", "grep", "write_file"),
                default=("read_file", "grep"),
            ),
        ),
    )

    assert set(scoped.tools) == {"read_file", "grep"}


def test_default_runtime_scopes_tools_to_leader_manifest(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(execution_engine="provider", model="opencode-go/glm-5.1"),
        graph=_StubGraph(),
    )
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    response = runtime.run(RuntimeRequest(prompt="go"))
    runtime_config_raw = dict(response.session.metadata)["runtime_config"]
    assert isinstance(runtime_config_raw, dict)
    runtime_config = cast(dict[str, object], runtime_config_raw)
    agent_raw = runtime_config["agent"]
    assert isinstance(agent_raw, dict)
    agent = cast(dict[str, object], agent_raw)

    assert agent["preset"] == "leader"
    assert any(event.event_type == "runtime.tool_lookup_succeeded" for event in response.events)
    assert all(event.payload.get("tool") != "missing_tool" for event in response.events)


def test_runtime_uses_session_local_tools_config_when_registry_was_disabled(
    tmp_path: Path,
) -> None:
    _write_local_tool_manifest(tmp_path)
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_LocalToolGraph(),
        config=RuntimeConfig(execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    effective_config = runtime._effective_runtime_config_from_metadata(  # pyright: ignore[reportPrivateUsage]
        {
            "runtime_config": {
                "execution_engine": "deterministic",
                "tools": {"local": {"enabled": True, "path": ".voidcode/tools"}},
            }
        }
    )

    registry = runtime._tool_registry_for_effective_config(  # pyright: ignore[reportPrivateUsage]
        effective_config
    )

    assert "local/echo" not in runtime._base_tool_registry.tools  # pyright: ignore[reportPrivateUsage]
    assert "local/echo" in registry.tools


def test_runtime_uses_session_local_tools_config_when_registry_was_enabled(
    tmp_path: Path,
) -> None:
    _write_local_tool_manifest(tmp_path)
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_LocalToolGraph(),
        config=RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(local=RuntimeToolsLocalConfig(enabled=True)),
        ),
        permission_policy=PermissionPolicy(mode="allow"),
    )
    effective_config = runtime._effective_runtime_config_from_metadata(  # pyright: ignore[reportPrivateUsage]
        {
            "runtime_config": {
                "execution_engine": "deterministic",
                "tools": {"local": {"enabled": False, "path": ".voidcode/tools"}},
            }
        }
    )

    registry = runtime._tool_registry_for_effective_config(  # pyright: ignore[reportPrivateUsage]
        effective_config
    )

    assert "local/echo" not in runtime._base_tool_registry.tools  # pyright: ignore[reportPrivateUsage]
    assert "local/echo" not in registry.tools


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


def test_tool_registry_rejects_duplicate_tool_names() -> None:
    with pytest.raises(ValueError, match="duplicate tool definition: injected_tool"):
        _ = ToolRegistry.from_tools((_InjectedTool(), _InjectedTool()))


def test_local_custom_tool_provider_discovers_opted_in_manifests(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path, read_only=False)

    tools = LocalCustomToolProvider(
        workspace=tmp_path,
        config=RuntimeToolsLocalConfig(enabled=True, path=".voidcode/tools"),
    ).provide_tools()

    assert len(tools) == 1
    tool = tools[0]
    assert tool.definition == ToolDefinition(
        name="local/echo",
        description="Echo arguments from a local manifest",
        input_schema={"type": "object", "properties": {"message": {"type": "string"}}},
        read_only=False,
    )


def test_local_custom_tool_provider_requires_opt_in(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path)

    assert LocalCustomToolProvider(workspace=tmp_path, config=None).provide_tools() == ()
    assert (
        LocalCustomToolProvider(
            workspace=tmp_path,
            config=RuntimeToolsLocalConfig(enabled=False, path=".voidcode/tools"),
        ).provide_tools()
        == ()
    )


def test_local_custom_tool_invokes_command_with_runtime_context(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path)
    tool = LocalCustomToolProvider(
        workspace=tmp_path,
        config=RuntimeToolsLocalConfig(enabled=True, path=".voidcode/tools"),
    ).provide_tools()[0]

    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="ses_local",
        )
    ):
        result = tool.invoke(
            ToolCall(tool_name="local/echo", arguments={"message": "hi"}), workspace=tmp_path
        )

    assert result.status == "ok"
    assert result.source == "local_custom_tool"
    assert result.content is not None
    payload = json.loads(result.content)
    assert payload["args"] == {"message": "hi"}
    assert payload["workspace"] == str(tmp_path.resolve())
    assert payload["session"] == "ses_local"


def test_local_custom_tool_timeout_polling_does_not_resend_stdin(tmp_path: Path) -> None:
    manifest = _write_local_tool_manifest(tmp_path)
    tools_dir = manifest.parent
    script = tools_dir / "slow_echo.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import time

            args = json.loads(sys.stdin.read() or "{}")
            time.sleep(0.12)
            print(json.dumps({"args": args}, sort_keys=True))
            """
        ),
        encoding="utf-8",
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["command"] = [sys.executable, "${manifest_dir}/slow_echo.py"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    tool = LocalCustomToolProvider(
        workspace=tmp_path,
        config=RuntimeToolsLocalConfig(enabled=True, path=".voidcode/tools"),
    ).provide_tools()[0]
    assert isinstance(tool, LocalCustomTool)

    result = tool.invoke_with_runtime_timeout(
        ToolCall(tool_name="local/echo", arguments={"message": "hi"}),
        workspace=tmp_path,
        timeout_seconds=1,
    )

    assert result.status == "ok"
    assert result.content is not None
    assert json.loads(result.content)["args"] == {"message": "hi"}


def test_runtime_includes_opted_in_local_custom_tools(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path)

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_StubGraph(),
        config=RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(local=RuntimeToolsLocalConfig(enabled=True)),
        ),
    )

    registry = runtime._tool_registry_for_effective_config(  # pyright: ignore[reportPrivateUsage]
        runtime._initial_effective_config  # pyright: ignore[reportPrivateUsage]
    )

    assert "local/echo" not in runtime._base_tool_registry.tools  # pyright: ignore[reportPrivateUsage]
    assert "local/echo" in registry.tools


def test_runtime_persists_top_level_local_tools_config(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path)
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_StubGraph(),
        config=RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(local=RuntimeToolsLocalConfig(enabled=True)),
        ),
    )

    metadata = runtime._runtime_config_metadata()  # pyright: ignore[reportPrivateUsage]

    assert metadata["tools"] == {"local": {"enabled": True, "path": ".voidcode/tools"}}
    effective = runtime._effective_runtime_config_from_metadata(  # pyright: ignore[reportPrivateUsage]
        {"runtime_config": metadata}
    )
    assert effective.tools == RuntimeToolsConfig(
        local=RuntimeToolsLocalConfig(enabled=True, path=".voidcode/tools")
    )


def test_runtime_rejects_local_custom_tool_name_collisions(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path, name="grep")
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_StubGraph(),
        config=RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(local=RuntimeToolsLocalConfig(enabled=True)),
        ),
    )

    with pytest.raises(ValueError, match="duplicate tool definition: grep"):
        _ = runtime._tool_registry_for_effective_config(  # pyright: ignore[reportPrivateUsage]
            runtime._initial_effective_config  # pyright: ignore[reportPrivateUsage]
        )


def test_local_custom_tool_provider_rejects_invalid_input_schema(tmp_path: Path) -> None:
    manifest = _write_local_tool_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["input_schema"] = {"type": "string"}
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="input_schema.type must be 'object'"):
        _ = LocalCustomToolProvider(
            workspace=tmp_path,
            config=RuntimeToolsLocalConfig(enabled=True, path=".voidcode/tools"),
        ).provide_tools()


class _LocalToolGraph:
    def __init__(self) -> None:
        self._step_count = 0

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep:
        _ = request, session
        if tool_results:
            return _StubStep(output=tool_results[-1].content or "done", is_finished=True)
        self._step_count += 1
        if self._step_count == 1:
            return _StubStep(
                tool_call=ToolCall(tool_name="local/echo", arguments={"message": "hi"})
            )
        return _StubStep(output="done", is_finished=True)


def test_local_custom_tools_require_approval_even_when_manifest_claims_read_only(
    tmp_path: Path,
) -> None:
    _write_local_tool_manifest(tmp_path, read_only=True)
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_LocalToolGraph(),
        config=RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(local=RuntimeToolsLocalConfig(enabled=True)),
        ),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    response = runtime.run(RuntimeRequest(prompt="go", session_id="local-approval"))
    snapshot = runtime.session_debug_snapshot(session_id="local-approval")

    assert response.session.status == "waiting"
    assert snapshot.pending_approval is not None
    assert snapshot.pending_approval.tool_name == "local/echo"
    assert snapshot.pending_approval.operation_class == "execute"


def test_runtime_resume_uses_persisted_local_tools_config(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path, read_only=True)
    initial_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_LocalToolGraph(),
        config=RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(local=RuntimeToolsLocalConfig(enabled=True)),
        ),
        permission_policy=PermissionPolicy(mode="ask"),
    )

    waiting = initial_runtime.run(RuntimeRequest(prompt="go", session_id="local-resume"))
    snapshot = initial_runtime.session_debug_snapshot(session_id="local-resume")
    assert waiting.session.status == "waiting"
    assert snapshot.pending_approval is not None

    resumed_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_LocalToolGraph(),
        config=RuntimeConfig(execution_engine="deterministic"),
        permission_policy=PermissionPolicy(mode="allow"),
    )

    resumed = resumed_runtime.resume(
        "local-resume",
        approval_request_id=snapshot.pending_approval.request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert resumed.output is not None
    assert json.loads(resumed.output)["args"] == {"message": "hi"}


def test_local_custom_tools_are_blocked_by_deny_policy(tmp_path: Path) -> None:
    _write_local_tool_manifest(tmp_path, read_only=True)
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        graph=_LocalToolGraph(),
        config=RuntimeConfig(
            execution_engine="deterministic",
            tools=RuntimeToolsConfig(local=RuntimeToolsLocalConfig(enabled=True)),
        ),
        permission_policy=PermissionPolicy(mode="deny"),
    )

    response = runtime.run(RuntimeRequest(prompt="go"))

    assert response.session.status == "running"
    assert any(
        event.event_type == "runtime.approval_resolved" and event.payload.get("decision") == "deny"
        for event in response.events
    )
    denied_event = next(
        event for event in response.events if event.event_type == "runtime.tool_completed"
    )
    assert denied_event.payload["tool"] == "local/echo"
    assert denied_event.payload["status"] == "error"
    assert denied_event.payload["permission_denied"] is True
    assert denied_event.payload["operation_class"] == "execute"


def test_runtime_registry_includes_discovered_mcp_tools(tmp_path: Path) -> None:
    class _StubMcpManager:
        def __init__(self) -> None:
            self.list_tools_calls = 0

        @property
        def configuration(self) -> McpConfigState:
            return McpConfigState(configured_enabled=True)

        def current_state(self) -> McpManagerState:
            return McpManagerState(mode="managed", configuration=self.configuration)

        def list_tools(
            self,
            *,
            workspace: Path,
            owner_session_id: str | None = None,
            parent_session_id: str | None = None,
        ):
            _ = workspace, owner_session_id, parent_session_id
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
            owner_session_id: str | None = None,
            parent_session_id: str | None = None,
        ) -> McpToolCallResult:
            _ = server_name, tool_name, arguments, workspace, owner_session_id, parent_session_id
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

        def list_tools(
            self,
            *,
            workspace: Path,
            owner_session_id: str | None = None,
            parent_session_id: str | None = None,
        ):
            _ = workspace, owner_session_id, parent_session_id
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
            owner_session_id: str | None = None,
            parent_session_id: str | None = None,
        ) -> McpToolCallResult:
            _ = server_name, tool_name, arguments, workspace, owner_session_id, parent_session_id
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
