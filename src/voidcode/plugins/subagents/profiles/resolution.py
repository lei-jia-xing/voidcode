"""Resolve executable child profiles for the task surfaces."""

from __future__ import annotations

from voidcode.capabilities.agent_queries import AgentDefinition, AgentDefinitionRequest
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import PluginQueryCaller

from .registry import AgentRegistry, load_agent_registry


def _load_registry(context: ToolContext) -> AgentRegistry:
    """The executable profile registry for this workspace, or a clear ingress refusal."""
    try:
        return load_agent_registry(context.require_workspace())
    except (ValueError, OSError) as exc:
        raise ValueError(f"agent profile registry is unavailable: {exc}") from exc


def _resolve_subagent(registry: AgentRegistry, subagent_type: str, *, where: str) -> AgentDefinition:
    """The executable child profile behind a parsed id, or a clear ingress refusal."""
    manifest = registry.get(subagent_type)
    if manifest is None:
        raise ValueError(f"{where} unknown subagent_type: {subagent_type!r}")
    return manifest


def _load_profile(context: ToolContext, subagent_type: str | None, *, where: str) -> AgentDefinition | None:
    """Resolve the selected profile, if any."""
    if subagent_type is None:
        return None
    return _resolve_subagent(_load_registry(context), subagent_type, where=where)


def agent_definition(request: AgentDefinitionRequest, caller: PluginQueryCaller) -> AgentDefinition | None:
    try:
        registry = load_agent_registry(caller.workspace)
    except (ValueError, OSError) as exc:
        raise ValueError(f"agent profile registry is unavailable: {exc}") from exc
    return registry.get(request.name)
