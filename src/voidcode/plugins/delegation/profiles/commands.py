"""Profile listing commands owned by the delegation plugin.

The profile model, discovery roots, builtin manifests and prompt materialization are ported
from the legacy ``voidcode.agent`` package (see ``models.py``/``registry.py``/``builtin.py``/
``prompts.py`` here, including the six verbatim ``texts/*.txt`` bodies).

``agents`` lists the registry and ``agents.show`` renders one profile's prompt plus its
declared tool allowlist: exactly the profiles ``task``/``task_batch`` resolve through
``subagent_type``. Nothing here selects a profile for a run, and no policy is silently
applied to a run.
"""

from __future__ import annotations

from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

from .models import AgentManifest
from .prompts import render_agent_prompt
from .registry import load_agent_manifest_registry


def _manifest_row(manifest: AgentManifest) -> dict[str, object]:
    return {
        "id": manifest.id,
        "name": manifest.name,
        "mode": manifest.mode,
        "description": manifest.description,
        "source_scope": manifest.source_scope,
        "source_path": manifest.source_path,
        "prompt_profile": manifest.prompt_profile,
        "model_preference": manifest.model_preference,
        "fallback_models": list(manifest.fallback_models),
        "tool_allowlist": list(manifest.tool_allowlist),
        "skill_refs": list(manifest.skill_refs),
        "top_level_selectable": manifest.top_level_selectable,
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
            registry = load_agent_manifest_registry(workspace)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))
        manifests = registry.list_manifests()
        rows = [_manifest_row(manifest) for manifest in manifests]
        lines = [
            f"- [{manifest.mode}] {manifest.id} — {manifest.description} ({manifest.source_scope}, tools: {len(manifest.tool_allowlist)})"
            for manifest in manifests
        ]
        return CommandResult(status="ok", output="\n".join(lines) or "No agent profiles found.", data={"profiles": rows})


class AgentShowCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="agents.show",
        description="Show one agent profile's rendered prompt and declared tool allowlist.",
        argument_schema={
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Agent profile id."}},
            "required": ["id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        raw_id = invocation.arguments.get("id")
        agent_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not agent_id:
            return CommandResult(status="error", error="agents.show requires a profile id")
        workspace = invocation.context.require_workspace()
        try:
            registry = load_agent_manifest_registry(workspace)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))
        manifest = registry.get(agent_id)
        if manifest is None:
            return CommandResult(status="error", error=f"unknown agent profile: {agent_id}")
        try:
            prompt = render_agent_prompt(manifest.prompt_materialization)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        data = _manifest_row(manifest)
        data["prompt"] = prompt
        return CommandResult(
            status="ok",
            output=prompt if prompt is not None else f"(no prompt materialization for {agent_id})",
            data=data,
        )


__all__ = ["AgentShowCommand", "AgentsCommand"]
