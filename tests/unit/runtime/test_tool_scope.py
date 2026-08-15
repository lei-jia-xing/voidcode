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
            _Tool("read", read_only=True),
            _Tool("write", read_only=False),
            _Tool("shell_exec", read_only=False),
            _Tool("memory_add", read_only=False),
        )
    )


def test_tool_scope_resolver_applies_agent_scope_before_runtime_policy() -> None:
    resolver = RuntimeToolScopeResolver(memory_enabled=True)
    agent = RuntimeAgentConfig(
        preset="leader",
        tools=RuntimeToolsConfig(allowlist=("read", "write")),
    )

    scoped = resolver.scope(_registry(), agent=agent, metadata={"mode": "analyze"})

    assert tuple(scoped.tools) == ("read",)


def test_tool_scope_resolver_uses_same_decision_for_schema_and_raw_call() -> None:
    resolver = RuntimeToolScopeResolver(memory_enabled=True)
    registry = _registry()
    metadata = {"mode": "plan"}

    scoped = resolver.scope(registry, agent=None, metadata=metadata)
    denial = resolver.denial(
        registry,
        agent=None,
        metadata=metadata,
        tool_name="write",
    )

    assert "write" not in scoped.tools
    assert denial is not None
    assert denial.reason == "read-only runtime policy denies mutating tools"
    assert denial.metadata() == {
        "tool": "write",
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
    assert "write" not in scoped.tools

    agent = RuntimeAgentConfig(
        preset="worker",
        manifest_tool_allowlist=("read",),
    )

    denial = RuntimeToolScopeResolver.delegation_policy_error(
        delegated_child=True,
        agent=agent,
        base_registry=_registry(),
        tool_name="write",
    )

    assert denial == (
        "delegation policy denied tool 'write' for child preset 'worker'; this preset may only call tools allowed by its manifest tool_allowlist"
    )


def test_tool_scope_resolver_does_not_claim_delegation_for_unknown_or_allowed_tools() -> None:
    agent = RuntimeAgentConfig(
        preset="worker",
        manifest_tool_allowlist=("read",),
    )

    assert (
        RuntimeToolScopeResolver.delegation_policy_error(
            delegated_child=True,
            agent=agent,
            base_registry=_registry(),
            tool_name="read",
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
