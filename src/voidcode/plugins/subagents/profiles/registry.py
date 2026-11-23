"""Load Markdown subagents from Claude Code's project and user locations."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from voidcode.capabilities.agent_queries import AgentDefinition, AgentSourceScope
from voidcode.capabilities.frontmatter import load_frontmatter_mapping, split_frontmatter

from .builtin import list_builtin_agents

_SUPPORTED_FIELDS = frozenset({"name", "description", "model", "maxTurns"})


@dataclass(frozen=True, slots=True)
class AgentRegistry:
    agents: Mapping[str, AgentDefinition]

    def __post_init__(self) -> None:
        object.__setattr__(self, "agents", MappingProxyType(dict(self.agents)))

    def get(self, name: str) -> AgentDefinition | None:
        return self.agents.get(name)


def user_agent_dir(env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    return Path(environment.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser() / "agents"


def project_agent_dir(workspace: Path) -> Path:
    return workspace.resolve() / ".claude" / "agents"


def load_agent_registry(workspace: Path, *, env: Mapping[str, str] | None = None) -> AgentRegistry:
    agents = {agent.name: agent for agent in list_builtin_agents()}
    for directory, scope in ((user_agent_dir(env), "user"), (project_agent_dir(workspace), "project")):
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError(f"agent path must be a directory: {directory}")
        seen: dict[str, Path] = {}
        for path in sorted(directory.rglob("*.md")):
            try:
                agent = agent_from_markdown(path.read_text(encoding="utf-8"), scope=scope, source_path=str(path))
            except (ValueError, OSError) as exc:
                raise ValueError(f"invalid agent {path}: {exc}") from exc
            if agent.name in seen:
                raise ValueError(f"duplicate agent name '{agent.name}' in {seen[agent.name]} and {path}")
            seen[agent.name] = path
            agents[agent.name] = agent
    return AgentRegistry(agents)


def agent_from_markdown(contents: str, *, scope: AgentSourceScope, source_path: str | None = None) -> AgentDefinition:
    frontmatter, body = split_frontmatter(contents, require_body=True)
    payload = load_frontmatter_mapping(frontmatter)
    unknown = set(payload) - _SUPPORTED_FIELDS
    if unknown:
        raise ValueError(f"unsupported agent field '{sorted(unknown)[0]}'")
    name = _text(payload, "name")
    if len(name) > 256 or ":" in name:
        raise ValueError("agent name must be at most 256 characters and contain no ':'")
    description = _text(payload, "description")
    if "model" in payload and payload["model"] != "inherit":
        raise ValueError("agent model currently supports only 'inherit'; per-agent model selection is not implemented")
    turns = payload.get("maxTurns")
    if "maxTurns" in payload and (type(turns) is not int or turns < 1):
        raise ValueError("agent maxTurns must be a positive integer")
    return AgentDefinition(
        name=name,
        description=description,
        prompt=body,
        source_scope=scope,
        source_path=source_path,
        max_turns=turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
    )


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"agent field '{field}' must be a non-empty string")
    return value.strip()
