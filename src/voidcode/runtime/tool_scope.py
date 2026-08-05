from __future__ import annotations

from fnmatch import fnmatchcase
from typing import cast

from .config import RuntimeAgentConfig
from .contracts import RuntimeRequestError
from .tool_provider import MEMORY_TOOL_NAMES, scoped_tool_registry_for_agent
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
        return tuple(
            self.decision(tool_name=name, registry=registry, metadata=metadata)
            for name in registry.tools
        )

    def decision(
        self,
        *,
        tool_name: str,
        registry: ToolRegistry,
        metadata: dict[str, object] | None,
    ) -> ToolPolicyDecision:
        mode = self.runtime_mode(metadata)
        read_only = self.effective_read_only(metadata)

        if tool_name in MEMORY_TOOL_NAMES and not self.memory_tools_allowed(metadata):
            return ToolPolicyDecision(
                tool_name=tool_name,
                allowed=False,
                mode=mode,
                read_only=read_only,
                decision="deny",
                reason="memory tools require explicit runtime memory policy allowance",
            )

        tool = registry.tools.get(tool_name)
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
        if any(
            fnmatchcase(tool_name, pattern) for pattern in agent.manifest_tool_allowlist if pattern
        ):
            return None
        if tool_name not in base_registry.tools:
            return None
        return (
            "delegation policy denied tool "
            f"'{tool_name}' for child preset '{agent.preset}'; this preset may only call "
            "tools allowed by its manifest tool_allowlist"
        )

    @staticmethod
    def runtime_mode(metadata: dict[str, object] | None) -> str:
        if metadata is None:
            return "normal"
        raw_mode = metadata.get("mode")
        if raw_mode in {"normal", "analyze", "plan"}:
            return cast(str, raw_mode)
        return "normal"

    @staticmethod
    def runtime_read_only(metadata: dict[str, object] | None) -> bool:
        if metadata is None:
            return False
        raw_mode = metadata.get("mode")
        if raw_mode in {"analyze", "plan"}:
            return True
        read_only = metadata.get("read_only", False)
        if not isinstance(read_only, bool):
            raise RuntimeRequestError("request metadata 'read_only' must be a boolean")
        return read_only

    def effective_read_only(self, metadata: dict[str, object] | None) -> bool:
        return self.runtime_read_only(metadata)

    def memory_tools_allowed(self, metadata: dict[str, object] | None) -> bool:
        if not self._memory_enabled or metadata is None:
            return False
        command = metadata.get("command")
        if isinstance(command, dict) and cast(dict[str, object], command).get("name") == "memory":
            return True
        return metadata.get("memory_tools_allowed") is True


__all__ = ["RuntimeToolScopeResolver"]
