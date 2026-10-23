from __future__ import annotations

from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

from .models import AgentDefinition
from .registry import load_agent_registry


def _agent_row(agent: AgentDefinition) -> dict[str, object]:
    return {
        "name": agent.name,
        "description": agent.description,
        "source_scope": agent.source_scope,
        "source_path": agent.source_path,
        "readonly": agent.readonly,
        "tools": None if agent.tools is None else list(agent.tools),
        "maxTurns": agent.max_turns,
    }


class AgentsCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="agents",
        description="List agent profiles (builtin and workspace/user discovered).",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        workspace = invocation.context.require_workspace()
        try:
            registry = load_agent_registry(workspace)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))
        agents = registry.list_agents()
        rows = [_agent_row(agent) for agent in agents]
        lines = [f"- {agent.name} — {agent.description} ({agent.source_scope}, readonly: {agent.readonly})" for agent in agents]
        return CommandResult(status="ok", output="\n".join(lines) or "No agent profiles found.", data={"profiles": rows})


class AgentShowCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="agents.show",
        description="Show one agent profile's rendered prompt and read-only stance.",
        argument_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Agent name."}},
            "required": ["name"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        raw_id = invocation.arguments.get("name")
        agent_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not agent_id:
            return CommandResult(status="error", error="agents.show requires an agent name")
        workspace = invocation.context.require_workspace()
        try:
            registry = load_agent_registry(workspace)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))
        agent = registry.get(agent_id)
        if agent is None:
            return CommandResult(status="error", error=f"unknown agent profile: {agent_id}")
        data = _agent_row(agent)
        data["prompt"] = agent.prompt
        return CommandResult(
            status="ok",
            output=agent.prompt,
            data=data,
        )


__all__ = ["AgentShowCommand", "AgentsCommand"]
