from __future__ import annotations

from typing import ClassVar

from voidcode.capabilities.agent_queries import AgentDefinition
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

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
        return CommandResult(
            status="ok",
            output="\n".join(lines) or "No agent profiles found.",
            data={"profiles": rows},
            presentation=CommandPresentation(
                title="Agent profiles",
                view_type="choices",
                data={
                    "options": [{"id": agent.name, "label": f"{agent.name} — {agent.description} ({agent.source_scope})"} for agent in agents],
                    "action": "agents.show",
                    "argument_name": "name",
                },
            )
            if agents
            else CommandPresentation(title="Agent profiles", view_type="notice", data={"message": "No agent profiles found."}),
        )


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

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = services
        agents = load_agent_registry(invocation.context.require_workspace()).list_agents()
        if not agents:
            return CommandPresentation(title="Agent profiles", view_type="notice", data={"message": "No agent profiles found."})
        return CommandPresentation(
            title="Inspect agent profile",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "name",
                        "label": "Agent profile",
                        "options": [{"id": agent.name, "label": f"{agent.name} — {agent.description}"} for agent in agents],
                    }
                ],
            },
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
            presentation=CommandPresentation(title=f"Agent: {agent.name}", view_type="markdown", data={"content": agent.prompt}),
        )


__all__ = ["AgentShowCommand", "AgentsCommand"]
