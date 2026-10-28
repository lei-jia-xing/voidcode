from __future__ import annotations

from importlib.resources import files

from voidcode.capabilities.agent_queries import AgentDefinition


def _prompt(name: str) -> str:
    return files(__package__).joinpath("texts", f"{name}.txt").read_text(encoding="utf-8").strip()


DEFAULT_AGENT = AgentDefinition(
    name="default",
    description="General user-facing assistant; runtime owns policy and tool scope.",
    prompt=_prompt("default"),
)

EXECUTE_AGENT = AgentDefinition(
    name="execute",
    description="Delegated execution of an assigned task; runtime enforces the active tool scope.",
    prompt=_prompt("execute"),
)

REVIEW_AGENT = AgentDefinition(
    name="review",
    readonly=True,
    description="Delegated review of evidence, risks, and options.",
    prompt=_prompt("review"),
)

EXPLORE_AGENT = AgentDefinition(
    name="explore",
    readonly=True,
    description="Delegated read-only discovery of relevant workspace evidence.",
    prompt=_prompt("explore"),
)

RESEARCH_AGENT = AgentDefinition(
    name="research",
    readonly=True,
    description="Delegated research using public sources and external evidence.",
    prompt=_prompt("research"),
)

PLAN_AGENT = AgentDefinition(
    name="plan",
    readonly=True,
    description="Delegated read-only planning grounded in the request and relevant evidence.",
    prompt=_prompt("plan"),
)


_AGENTS = (
    DEFAULT_AGENT,
    EXECUTE_AGENT,
    REVIEW_AGENT,
    EXPLORE_AGENT,
    RESEARCH_AGENT,
    PLAN_AGENT,
)


def list_builtin_agents() -> tuple[AgentDefinition, ...]:
    return _AGENTS
