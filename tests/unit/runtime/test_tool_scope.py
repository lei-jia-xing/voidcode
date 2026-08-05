from __future__ import annotations

from pathlib import Path

from voidcode.runtime.config import RuntimeAgentConfig, RuntimeToolsConfig
from voidcode.runtime.tool_registry import ToolRegistry
from voidcode.runtime.tool_scope import RuntimeToolScopeResolver
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult


class _Tool:
    def __init__(self, name: str, *, read_only: bool) -> None:
        self._definition = ToolDefinition(
            name=name,
            description=name,
            input_schema={"type": "object"},
            read_only=read_only,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        return ToolResult(content="ok")


def _registry() -> ToolRegistry:
    return ToolRegistry.from_tools(
        (
            _Tool("read_file", read_only=True),
            _Tool("write_file", read_only=False),
            _Tool("shell_exec", read_only=False),
            _Tool("memory_add", read_only=False),
        )
    )


def test_tool_scope_resolver_applies_agent_scope_before_runtime_policy() -> None:
    resolver = RuntimeToolScopeResolver(memory_enabled=True)
    agent = RuntimeAgentConfig(
        preset="leader",
        tools=RuntimeToolsConfig(allowlist=("read_file", "write_file")),
    )

    scoped = resolver.scope(_registry(), agent=agent, metadata={"mode": "analyze"})

    assert tuple(scoped.tools) == ("read_file",)


def test_tool_scope_resolver_uses_same_decision_for_schema_and_raw_call() -> None:
    resolver = RuntimeToolScopeResolver(memory_enabled=True)
    registry = _registry()
    metadata = {"mode": "plan"}

    scoped = resolver.scope(registry, agent=None, metadata=metadata)
    denial = resolver.denial(
        registry,
        agent=None,
        metadata=metadata,
        tool_name="write_file",
    )

    assert "write_file" not in scoped.tools
    assert denial is not None
    assert denial.reason == "read-only runtime policy denies mutating tools"
    assert denial.metadata() == {
        "tool": "write_file",
        "mode": "plan",
        "read_only": True,
        "decision": "deny",
        "reason": "read-only runtime policy denies mutating tools",
    }


def test_tool_scope_resolver_preserves_shell_for_command_level_classification() -> None:
    scoped = RuntimeToolScopeResolver(memory_enabled=True).scope(
        _registry(),
        agent=None,
        metadata={"read_only": True},
    )

    assert "shell_exec" in scoped.tools
    assert "write_file" not in scoped.tools


def test_tool_scope_resolver_honors_workflow_read_only_default_and_mode() -> None:
    resolver = RuntimeToolScopeResolver(memory_enabled=True)
    metadata = {
        "workflow": {
            "read_only_default": True,
            "effective": {"mode": "review"},
        }
    }

    denial = resolver.denial(
        _registry(),
        agent=None,
        metadata=metadata,
        tool_name="write_file",
    )

    assert denial is not None
    assert denial.mode == "review"
    assert denial.read_only is True


def test_tool_scope_resolver_exposes_memory_only_in_explicit_memory_context() -> None:
    resolver = RuntimeToolScopeResolver(memory_enabled=True)

    default = resolver.scope(_registry(), agent=None, metadata=None)
    memory_command = resolver.scope(
        _registry(),
        agent=None,
        metadata={"command": {"name": "memory"}},
    )

    assert "memory_add" not in default.tools
    assert "memory_add" in memory_command.tools


def test_tool_scope_resolver_never_exposes_memory_when_runtime_disabled() -> None:
    resolver = RuntimeToolScopeResolver(memory_enabled=False)

    scoped = resolver.scope(
        _registry(),
        agent=None,
        metadata={"memory_tools_allowed": True},
    )

    assert "memory_add" not in scoped.tools


def test_tool_scope_resolver_reports_delegated_manifest_denial_for_known_tools() -> None:
    agent = RuntimeAgentConfig(
        preset="worker",
        manifest_tool_allowlist=("read_file",),
    )

    denial = RuntimeToolScopeResolver.delegation_policy_error(
        delegated_child=True,
        agent=agent,
        base_registry=_registry(),
        tool_name="write_file",
    )

    assert denial == (
        "delegation policy denied tool 'write_file' for child preset 'worker'; this preset "
        "may only call tools allowed by its manifest tool_allowlist"
    )


def test_tool_scope_resolver_does_not_claim_delegation_for_unknown_or_allowed_tools() -> None:
    agent = RuntimeAgentConfig(
        preset="worker",
        manifest_tool_allowlist=("read_file",),
    )

    assert (
        RuntimeToolScopeResolver.delegation_policy_error(
            delegated_child=True,
            agent=agent,
            base_registry=_registry(),
            tool_name="read_file",
        )
        is None
    )
    assert (
        RuntimeToolScopeResolver.delegation_policy_error(
            delegated_child=True,
            agent=agent,
            base_registry=_registry(),
            tool_name="not_registered",
        )
        is None
    )
