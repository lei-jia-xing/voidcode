from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from ..tools.contracts import Tool, ToolDefinition
from ..tools.guidance import definition_with_guidance
from .config import RuntimeHooksConfig
from .tool_provider import BuiltinToolProvider


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
        format_tool: Tool | None = None,
        mcp_tools: tuple[Tool, ...] = (),
        hooks_config: RuntimeHooksConfig | None = None,
        skill_tool: Tool | None = None,
        task_tool: Tool | None = None,
        question_tool: Tool | None = None,
        background_output_tool: Tool | None = None,
        background_cancel_tool: Tool | None = None,
        background_retry_tool: Tool | None = None,
    ) -> ToolRegistry:
        return cls.from_tools(
            BuiltinToolProvider(
                lsp_tool=lsp_tool,
                format_tool=format_tool,
                mcp_tools=mcp_tools,
                hooks_config=hooks_config,
                skill_tool=skill_tool,
                task_tool=task_tool,
                question_tool=question_tool,
                background_output_tool=background_output_tool,
                background_cancel_tool=background_cancel_tool,
                background_retry_tool=background_retry_tool,
            ).provide_tools()
        )

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(definition_with_guidance(tool.definition) for tool in self.tools.values())

    def resolve(self, tool_name: str) -> Tool:
        try:
            return self.tools[tool_name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_name}") from exc

    def filtered(self, patterns: Iterable[str]) -> ToolRegistry:
        normalized_patterns = tuple(pattern for pattern in patterns if pattern)
        return ToolRegistry(
            tools={
                name: tool
                for name, tool in self.tools.items()
                if any(fnmatchcase(name, pattern) for pattern in normalized_patterns)
            }
        )

    def excluding(self, tool_names: Iterable[str]) -> ToolRegistry:
        excluded = frozenset(tool_names)
        return ToolRegistry(
            tools={name: tool for name, tool in self.tools.items() if name not in excluded}
        )
