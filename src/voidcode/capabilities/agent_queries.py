"""Shared named-agent definition contract, independent of the delegation plugin.

QueryUnavailable means no implementation; None means no such agent.
Malformed answers are contract violations, never an unknown agent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.plugin import PluginQuery, PluginQueryCaller
from voidcode.sdk.services import PluginServices, QueryContractError

AGENT_DEFINITION_QUERY = "workspace.agent_definition"

type AgentSourceScope = Literal["builtin", "project", "user"]

_AGENT_KEYS = frozenset({"name", "description", "prompt", "source_scope", "source_path", "readonly", "tools", "max_turns"})


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """The executable subset of a Markdown subagent definition plus its source metadata."""

    name: str
    description: str
    prompt: str
    source_scope: AgentSourceScope = "builtin"
    source_path: str | None = None
    readonly: bool = False
    tools: tuple[str, ...] | None = None
    max_turns: int | None = None

    @classmethod
    def from_query(cls, value: Mapping[str, object], *, name: str) -> AgentDefinition | None:
        if set(value) != _AGENT_KEYS or value.get("name") != name:
            return None
        description = value.get("description")
        prompt = value.get("prompt")
        source_scope = value.get("source_scope")
        source_path = value.get("source_path")
        readonly = value.get("readonly")
        tools = value.get("tools")
        max_turns = value.get("max_turns")
        if not isinstance(description, str) or not description.strip():
            return None
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        if source_scope == "builtin":
            scope: AgentSourceScope = "builtin"
        elif source_scope == "project":
            scope = "project"
        elif source_scope == "user":
            scope = "user"
        else:
            return None
        if source_path is None:
            resolved_path: str | None = None
        elif isinstance(source_path, str) and source_path:
            resolved_path = source_path
        else:
            return None
        if not isinstance(readonly, bool):
            return None
        resolved_tools: tuple[str, ...] | None = None
        if tools is not None:
            if not isinstance(tools, (list, tuple)) or not tools:
                return None
            cleaned: list[str] = []
            for tool in tools:
                if not isinstance(tool, str) or not tool.strip():
                    return None
                cleaned.append(tool)
            resolved_tools = tuple(cleaned)
        if max_turns is not None:
            if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
                return None
        return cls(
            name=name,
            description=description,
            prompt=prompt,
            source_scope=scope,
            source_path=resolved_path,
            readonly=readonly,
            tools=resolved_tools,
            max_turns=max_turns,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "source_scope": self.source_scope,
            "source_path": self.source_path,
            "readonly": self.readonly,
            "tools": None if self.tools is None else list(self.tools),
            "max_turns": self.max_turns,
        }


class AgentDefinitionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str = Field(min_length=1)


class _AgentDefinitionQuery:
    name: str = AGENT_DEFINITION_QUERY

    def ask(self, services: PluginServices, request: AgentDefinitionRequest) -> AgentDefinition | None:
        value = services.query(self.name, request.model_dump())
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise QueryContractError(f"query {self.name!r} must answer with a JSON object")
        agent = AgentDefinition.from_query(value, name=request.name)
        if agent is None:
            raise QueryContractError(f"query {self.name!r} answered outside its request/answer contract")
        return agent

    def bind(self, handler: Callable[[AgentDefinitionRequest, PluginQueryCaller], AgentDefinition | None]) -> PluginQuery:
        def invoke(arguments: Mapping[str, object], caller: PluginQueryCaller) -> Mapping[str, object] | None:
            req = AgentDefinitionRequest.model_validate(arguments)
            ans = handler(req, caller)
            return None if ans is None else ans.as_payload()

        return PluginQuery(name=self.name, handler=invoke)


AGENT_DEFINITIONS = _AgentDefinitionQuery()


def apply_agent_profile(
    profile: AgentDefinition | None,
    prompt: str,
    tool_allowlist: tuple[str, ...] | None,
    max_steps: int,
) -> tuple[str, bool, tuple[str, ...] | None, int]:
    """Effective child prompt, stance, scope, and budget behind an optional agent profile."""
    if profile is None:
        return prompt, False, tool_allowlist, max_steps
    body = f"{profile.prompt}\n\n{prompt}"
    if profile.tools is not None:
        tool_allowlist = profile.tools if tool_allowlist is None else tuple(name for name in tool_allowlist if name in profile.tools)
        if not tool_allowlist:
            raise ValueError(f"agent '{profile.name}' has no tools within the requested scope")
    if profile.max_turns is not None:
        max_steps = min(max_steps, profile.max_turns)
    return body, profile.readonly, tool_allowlist, max_steps
