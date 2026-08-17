from __future__ import annotations

from fnmatch import fnmatchcase

from .config import RuntimeAgentConfig
from .contracts import runtime_mode_from_metadata, runtime_read_only_from_metadata
from .tool_provider import scoped_tool_registry_for_agent
from .tool_registry import ToolPolicyDecision, ToolRegistry


class RuntimeToolScopeResolver:
    """Materialize agent-visible tools and matching raw-call policy decisions."""

    def __init__(self, *, memory_enabled: bool) -> None:
        self._memory_enabled = memory_enabled

    def scope(
        self,
        registry: ToolRegistry,
        *,
        agent: RuntimeAgentConfig | None,
        metadata: dict[str, object] | None,
    ) -> ToolRegistry:
        agent_scoped = scoped_tool_registry_for_agent(registry, agent=agent)
        return self.apply_policy(agent_scoped, metadata=metadata)

    def apply_policy(
        self,
        registry: ToolRegistry,
        *,
        metadata: dict[str, object] | None,
    ) -> ToolRegistry:
        return registry.allowed_by_policy(self.decisions(registry, metadata=metadata))

    def decisions(
        self,
        registry: ToolRegistry,
        *,
        metadata: dict[str, object] | None,
    ) -> tuple[ToolPolicyDecision, ...]:
        return tuple(self.decision(tool_name=name, registry=registry, metadata=metadata) for name in registry.tools)

    def decision(
        self,
        *,
        tool_name: str,
        registry: ToolRegistry,
        metadata: dict[str, object] | None,
    ) -> ToolPolicyDecision:
        # Single shared derivation: mode -> read_only (including explicit
        # read_only metadata) comes from mode.py's resolve_mode via the
        # contracts wrappers; this resolver no longer keeps a private copy.
        mode = runtime_mode_from_metadata(metadata)
        read_only = runtime_read_only_from_metadata(metadata)

        tool = registry.tools.get(tool_name)
        # shell_exec stays callable under the read-only stance: read-only shell
        # commands (git status, tests, etc.) remain available, while mutating
        # uses are still denied by the permission layer's operation-class
        # defense in depth.
        if read_only and tool_name == "shell_exec":
            return ToolPolicyDecision(
                tool_name=tool_name,
                allowed=True,
                mode=mode,
                read_only=read_only,
                decision="allow",
            )
        if read_only and tool is not None and not tool.definition.read_only:
            return ToolPolicyDecision(
                tool_name=tool_name,
                allowed=False,
                mode=mode,
                read_only=read_only,
                decision="deny",
                reason="read-only runtime policy denies mutating tools",
            )
        return ToolPolicyDecision(
            tool_name=tool_name,
            allowed=True,
            mode=mode,
            read_only=read_only,
            decision="allow",
        )

    def denial(
        self,
        registry: ToolRegistry,
        *,
        agent: RuntimeAgentConfig | None,
        metadata: dict[str, object] | None,
        tool_name: str,
    ) -> ToolPolicyDecision | None:
        agent_scoped = scoped_tool_registry_for_agent(registry, agent=agent)
        if tool_name not in agent_scoped.tools:
            return None
        decision = self.decision(
            tool_name=tool_name,
            registry=agent_scoped,
            metadata=metadata,
        )
        return None if decision.allowed else decision

    @staticmethod
    def delegation_policy_error(
        *,
        delegated_child: bool,
        agent: RuntimeAgentConfig | None,
        base_registry: ToolRegistry,
        tool_name: str,
    ) -> str | None:
        if not delegated_child or agent is None or not agent.manifest_tool_allowlist:
            return None
        if any(fnmatchcase(tool_name, pattern) for pattern in agent.manifest_tool_allowlist if pattern):
            return None
        if tool_name not in base_registry.tools:
            return None
        return (
            "delegation policy denied tool "
            f"'{tool_name}' for child preset '{agent.preset}'; this preset may only call "
            "tools allowed by its manifest tool_allowlist"
        )


def tool_policy_error(decision: ToolPolicyDecision) -> str:
    reason = decision.reason or "runtime tool policy denied the tool"
    return f"{reason}: '{decision.tool_name}'"


__all__ = ["RuntimeToolScopeResolver", "tool_policy_error"]
