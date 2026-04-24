from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type AgentManifestId = Literal[
    "leader",
    "worker",
    "advisor",
    "explore",
    "researcher",
    "product",
]
type AgentMode = Literal["primary", "subagent", "all"]
type AgentExecutionEngineName = Literal["deterministic", "provider"]
type AgentManifestFieldSemantic = Literal["live_default", "intent"]


@dataclass(frozen=True, slots=True)
class AgentManifest:
    id: AgentManifestId
    name: str
    mode: AgentMode
    description: str
    prompt_profile: str | None = None
    execution_engine: AgentExecutionEngineName | None = None
    model_preference: str | None = None
    tool_allowlist: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    routing_hints: dict[str, object] = field(default_factory=dict)

    @property
    def live_default_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self.prompt_profile is not None:
            fields.append("prompt_profile")
        if self.execution_engine is not None:
            fields.append("execution_engine")
        if self.model_preference is not None:
            fields.append("model_preference")
        if self.tool_allowlist:
            fields.append("tool_allowlist")
        if self.skill_refs:
            fields.append("skill_refs")
        return tuple(fields)

    @property
    def intent_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self.routing_hints:
            fields.append("routing_hints")
        return tuple(fields)

    def field_semantic(self, field_name: str) -> AgentManifestFieldSemantic:
        if field_name in self.live_default_fields:
            return "live_default"
        if field_name in self.intent_fields:
            return "intent"
        raise ValueError(f"unknown or unset manifest field semantic: {field_name}")
