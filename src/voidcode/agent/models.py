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
