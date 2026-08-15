from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from ..tools.contracts import Tool, ToolDefinition
from ..tools.guidance import definition_with_guidance
from .config import RuntimeAgentConfig, RuntimeHooksConfig
from .edit_schema_policy import EditSchemaResolver
from .tool_provider import BuiltinToolProvider

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
        "question",
        "todo_write",
        # Skill loading is a first-class runtime mechanism.
        "skill",
        # Terminal output contract: the graph loop completes on submit_result.
        "submit_result",
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
    if agent.manifest_tool_allowlist:
        patterns.extend(agent.manifest_tool_allowlist)
    if agent.tools is not None:
        if agent.tools.allowlist is not None:
            patterns.extend(agent.tools.allowlist)
        if agent.tools.default is not None:
            patterns.extend(agent.tools.default)
    return tuple(patterns)


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
    """Small in-memory registry used by the runtime boundary."""

    tools: dict[str, Tool] = field(default_factory=dict)

    @classmethod
    def from_tools(cls, tools: Iterable[Tool]) -> ToolRegistry:
        registry: dict[str, Tool] = {}
        for tool in tools:
            name = tool.definition.name
            if name in registry:
                raise ValueError(f"duplicate tool definition: {name}")
            registry[name] = tool
        return cls(tools=registry)

    @classmethod
    def with_defaults(
        cls,
        *,
        lsp_tool: Tool | None = None,
        mcp_tools: tuple[Tool, ...] = (),
        hooks_config: RuntimeHooksConfig | None = None,
        edit_schema_resolver: EditSchemaResolver | None = None,
        skill_tool: Tool | None = None,
        task_tool: Tool | None = None,
        question_tool: Tool | None = None,
        background_output_tool: Tool | None = None,
        background_cancel_tool: Tool | None = None,
        background_process_start_tool: Tool | None = None,
        background_process_logs_tool: Tool | None = None,
        background_process_stop_tool: Tool | None = None,
        background_process_send_tool: Tool | None = None,
    ) -> ToolRegistry:
        return cls.from_tools(
            BuiltinToolProvider(
                lsp_tool=lsp_tool,
                mcp_tools=mcp_tools,
                hooks_config=hooks_config,
                edit_schema_resolver=edit_schema_resolver,
                skill_tool=skill_tool,
                task_tool=task_tool,
                question_tool=question_tool,
                background_output_tool=background_output_tool,
                background_cancel_tool=background_cancel_tool,
                background_process_start_tool=background_process_start_tool,
                background_process_logs_tool=background_process_logs_tool,
                background_process_stop_tool=background_process_stop_tool,
                background_process_send_tool=background_process_send_tool,
            ).provide_tools()
        )

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition_with_guidance(tool.definition) for tool in self.tools.values())

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
        patterns = tuple(allowlist_patterns)
        visible = (
            tool
            for tool in self.tools.values()
            if tool.definition.name in ESSENTIAL_TOOL_NAMES or tool_required_by_allowlist_patterns(tool.definition.name, patterns)
        )
        return tuple(definition_with_guidance(tool.definition) for tool in visible)

    def resolve(self, tool_name: str) -> Tool:
        try:
            return self.tools[tool_name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_name}") from exc

    def filtered(self, patterns: Iterable[str]) -> ToolRegistry:
        normalized_patterns = tuple(pattern for pattern in patterns if pattern)
        return ToolRegistry(
            tools={name: tool for name, tool in self.tools.items() if any(fnmatchcase(name, pattern) for pattern in normalized_patterns)}
        )

    def excluding(self, tool_names: Iterable[str]) -> ToolRegistry:
        excluded = frozenset(tool_names)
        return ToolRegistry(tools={name: tool for name, tool in self.tools.items() if name not in excluded})

    def allowed_by_policy(self, policy: Iterable[ToolPolicyDecision]) -> ToolRegistry:
        allowed_names = frozenset(decision.tool_name for decision in policy if decision.allowed)
        return ToolRegistry(tools={name: tool for name, tool in self.tools.items() if name in allowed_names})
