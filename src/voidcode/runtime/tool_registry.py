from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from types import MappingProxyType
from typing import Literal

from ..tools.contracts import Tool, ToolDefinition, ToolEffect, is_read_tier
from .config import RuntimeAgentConfig

#: Tools always shown top-level in the provider tools array when the
#: essential/discoverable split is enabled. Everything not in this set is
#: discoverable: reachable on demand through ``voidcode://tool/<name>`` doc
#: reads (via read) and ``invoke_tool`` dispatch. The dispatch/read
#: mechanisms themselves MUST stay essential or discoverable tools become
#: unreachable.
ESSENTIAL_TOOL_NAMES = frozenset(
    {
        # Core workspace navigation and edit loop.
        "read",
        "edit",
        "write",
        "grep",
        "glob",
        "shell_exec",
        # Delegation, clarification, and progress state.
        "task",
        "task_batch",
        "question",
        "todo",
        # Skill loading is a first-class runtime mechanism.
        "skill",
        # Terminal output contract: the graph loop completes on yield.
        "yield",
        # On-demand access mechanisms (dispatch + doc read).
        "invoke_tool",
    }
)


def tool_required_by_allowlist_patterns(
    tool_name: str,
    patterns: Iterable[str],
) -> bool:
    """Whether an explicit allowlist pattern forces a tool to stay top-level."""
    return any(fnmatchcase(tool_name, pattern) for pattern in patterns if pattern)


def agent_required_tool_patterns(agent: RuntimeAgentConfig | None) -> tuple[str, ...]:
    """Allowlist patterns from an agent manifest / request tool config.

    Tools matching these patterns were explicitly selected for the session, so
    they must stay visible top-level even when the essential/discoverable
    split is enabled.
    """
    if agent is None:
        return ()
    patterns: list[str] = []
    internal = agent.runtime_internal if agent is not None else None
    if internal is not None:
        patterns.extend(internal.manifest_tool_allowlist)
    if agent.tools is not None:
        if agent.tools.allowlist is not None:
            patterns.extend(agent.tools.allowlist)
        if agent.tools.default is not None:
            patterns.extend(agent.tools.default)
    return tuple(patterns)


@dataclass(frozen=True, slots=True)
class ToolCatalogEntry:
    """Deterministic, fact-only projection of a declared registry tool."""

    name: str
    visibility: Literal["essential", "discoverable"]
    read_only: bool
    documentation_uri: str
    replay_policy: Literal["safe", "never"]


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    tool_name: str
    allowed: bool
    mode: str
    read_only: bool
    decision: str
    reason: str | None = None

    def metadata(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "tool": self.tool_name,
            "mode": self.mode,
            "read_only": self.read_only,
            "decision": self.decision,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass(slots=True)
class ToolRegistry:
    """Declared metadata and genuine dispatch instances, with explicit binding."""

    declarations: Mapping[str, ToolDefinition] = field(default_factory=lambda: MappingProxyType({}))
    tools: dict[str, Tool] = field(default_factory=dict)

    @classmethod
    def from_definitions(cls, definitions: Iterable[ToolDefinition]) -> ToolRegistry:
        declarations: dict[str, ToolDefinition] = {}
        for definition in definitions:
            owned = _validated_definition(definition)
            if owned.name in declarations:
                raise ValueError(f"duplicate tool definition: {owned.name}")
            declarations[owned.name] = owned
        return cls(declarations=MappingProxyType(declarations))

    @classmethod
    def from_tools(cls, tools: Iterable[Tool]) -> ToolRegistry:
        """Register genuine already-constructed tools at an explicit consumer boundary."""
        instances = tuple(tools)
        registry = cls.from_definitions(tool.definition for tool in instances)
        return cls(declarations=registry.declarations, tools=dict(zip(registry.declarations, instances, strict=True)))

    def definition(self, name: str) -> ToolDefinition | None:
        """Passively inspect one declaration; never resolve or activate a tool."""
        return self.declarations.get(name)

    def bind(self, materialize: Callable[[ToolDefinition], Tool]) -> ToolRegistry:
        """Bind missing instances through the caller's captured activated factory."""
        tools = dict(self.tools)
        for name, definition in self.declarations.items():
            if name in tools:
                continue
            tool = materialize(definition)
            if not callable(getattr(tool, "invoke", None)) or _validated_definition(tool.definition) != definition:
                raise ValueError(f"materialized tool does not match declaration: {name}")
            tools[name] = tool
        return ToolRegistry(declarations=self.declarations, tools=tools)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self.declarations.values())

    def _provider_definitions(
        self,
        *,
        essential_only: bool,
        allowlist_patterns: Iterable[str] = (),
    ) -> tuple[ToolDefinition, ...]:
        if not essential_only:
            return self.definitions()
        patterns = tuple(allowlist_patterns)
        return tuple(
            definition
            for definition in self.declarations.values()
            if definition.name in ESSENTIAL_TOOL_NAMES or tool_required_by_allowlist_patterns(definition.name, patterns)
        )

    def provider_definitions(
        self,
        *,
        allowlist_patterns: Iterable[str] = (),
    ) -> tuple[ToolDefinition, ...]:
        """Provider-visible definitions under the essential/discoverable split.

        Only essential tools (plus any tool explicitly selected by an agent
        allowlist pattern) are exposed top-level; the rest remain registered
        and dispatchable via ``invoke_tool``.
        """
        return self._provider_definitions(essential_only=True, allowlist_patterns=allowlist_patterns)

    def capability_catalog(
        self,
        *,
        essential_only: bool = False,
        allowlist_patterns: Iterable[str] = (),
    ) -> tuple[ToolCatalogEntry, ...]:
        """Project the same declared provider scope into deterministic catalog rows."""
        entries = tuple(
            ToolCatalogEntry(
                name=definition.name,
                visibility=("essential" if definition.name in ESSENTIAL_TOOL_NAMES else "discoverable"),
                read_only=is_read_tier(definition.effects),
                documentation_uri=f"voidcode://tool/{definition.name}",
                replay_policy=definition.effective_replay_policy,
            )
            for definition in self._provider_definitions(
                essential_only=essential_only,
                allowlist_patterns=allowlist_patterns,
            )
        )
        return tuple(sorted(entries, key=lambda entry: entry.name))

    def capability_catalog_prompt(
        self,
        *,
        essential_only: bool = False,
        allowlist_patterns: Iterable[str] = (),
    ) -> str:
        """Render a stable, fact-only catalog section for provider prompts."""
        entries = self.capability_catalog(
            essential_only=essential_only,
            allowlist_patterns=allowlist_patterns,
        )
        lines = ["Runtime tool catalog (facts only; not authorization):"]
        for entry in entries:
            lines.extend(
                (
                    f"- name: {entry.name}",
                    f"  visibility: {entry.visibility}",
                    f"  read_only: {str(entry.read_only).lower()}",
                    f"  replay_policy: {entry.replay_policy}",
                    f"  documentation: {entry.documentation_uri}",
                )
            )
        return "\n".join(lines)

    def resolve(self, tool_name: str) -> Tool:
        if tool_name not in self.declarations:
            raise ValueError(f"unknown tool: {tool_name}")
        tool = self.tools.get(tool_name)
        if tool is None:
            raise ValueError(f"tool is not bound: {tool_name}")
        return tool

    def _selected(self, names: Iterable[str]) -> ToolRegistry:
        selected = frozenset(names)
        return ToolRegistry(
            declarations=MappingProxyType({name: definition for name, definition in self.declarations.items() if name in selected}),
            tools={name: tool for name, tool in self.tools.items() if name in selected},
        )

    def filtered(self, patterns: Iterable[str]) -> ToolRegistry:
        normalized_patterns = tuple(pattern for pattern in patterns if pattern)
        return self._selected(name for name in self.declarations if any(fnmatchcase(name, pattern) for pattern in normalized_patterns))

    def excluding(self, tool_names: Iterable[str]) -> ToolRegistry:
        excluded = frozenset(tool_names)
        return self._selected(name for name in self.declarations if name not in excluded)

    def allowed_by_policy(self, policy: Iterable[ToolPolicyDecision]) -> ToolRegistry:
        return self._selected(decision.tool_name for decision in policy if decision.allowed)


def _validated_definition(definition: ToolDefinition) -> ToolDefinition:
    if not isinstance(definition, ToolDefinition):
        raise ValueError("tool declaration must be a ToolDefinition")
    if not isinstance(definition.name, str) or not definition.name:
        raise ValueError("tool declaration name must be a non-empty string")
    if not isinstance(definition.description, str):
        raise ValueError("tool declaration description must be a string")
    if not isinstance(definition.input_schema, Mapping):
        raise ValueError("tool declaration input_schema must be an object")
    if not isinstance(definition.effects, frozenset) or any(not isinstance(effect, ToolEffect) for effect in definition.effects):
        raise ValueError("tool declaration effects must be ToolEffect values")
    if not isinstance(definition.path_argument_keys, tuple) or any(not isinstance(key, str) for key in definition.path_argument_keys):
        raise ValueError("tool declaration path_argument_keys must be strings")
    if definition.replay_policy not in (None, "safe", "never"):
        raise ValueError("tool declaration replay_policy must be safe or never")
    return definition
