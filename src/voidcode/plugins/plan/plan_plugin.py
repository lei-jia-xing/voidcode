"""Plan plugin: read-only policy overlay and contextual planning instructions."""

from __future__ import annotations

from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

PLAN_MODE_INSTRUCTION = (
    "Plan mode is active. You are in a persistent conversational planning state. "
    "External mutation is forbidden: tools declaring WRITE or EXECUTE effects are unavailable and calls are denied. "
    "Read-only exploration, session-scoped bookkeeping, network reads, and read-only subagent delegation remain available. "
    "Deliver a structured, actionable implementation plan in your response. "
    "The user can exit plan mode at any time using /plan off."
)

ENABLED_OUTPUT = (
    "Plan mode enabled. External mutation (WRITE/EXECUTE) is forbidden; reads, session bookkeeping, "
    "network access, and read-only delegation remain available. Use /plan off to exit."
)
DISABLED_OUTPUT = "Plan mode disabled."
READ_ONLY_AFTER_PLAN = "The session is still read-only for another reason (frozen policy or another owner's overlay)."


def _mode_action(active: bool) -> CommandPresentation:
    return CommandPresentation(
        title="Plan mode active · Submit to leave plan mode" if active else "Plan mode inactive · Submit to enter plan mode",
        view_type="form",
        data={"fields": [], "action": "plan.off" if active else "plan"},
    )


def _status_output(active: bool, session_read_only: bool) -> str:
    if active:
        return "Plan mode: active"
    if session_read_only:
        return f"Plan mode: inactive\n{READ_ONLY_AFTER_PLAN}"
    return "Plan mode: inactive"


class PlanCommand:
    """Enter plan mode: tighten the session with this owner's read-only overlay."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="plan",
        description="Enter plan mode: read-only exploration plus a structured plan.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        if services.overlay_active():
            return CommandResult(status="ok", output=ENABLED_OUTPUT, data={"active": True, "changed": False}, presentation=_mode_action(True))
        services.set_overlay(tier="read-only")
        services.stage_event("plan_mode_enabled", {})
        return CommandResult(status="ok", output=ENABLED_OUTPUT, data={"active": True, "changed": True}, presentation=_mode_action(True))


class PlanOffCommand:
    """Leave plan mode: revoke this owner's overlay only."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="plan.off",
        description="Leave plan mode.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        cleared = services.clear_overlay()
        if cleared:
            services.stage_event("plan_mode_disabled", {})
        output = DISABLED_OUTPUT if not services.session_read_only() else f"{DISABLED_OUTPUT}\n{READ_ONLY_AFTER_PLAN}"
        return CommandResult(status="ok", output=output, data={"active": False, "changed": cleared}, presentation=_mode_action(False))


class PlanStatusCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="plan.status",
        description="Show whether plan mode is active.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        active = services.overlay_active()
        session_read_only = services.session_read_only()
        return CommandResult(
            status="ok",
            output=_status_output(active, session_read_only),
            data={"active": active, "read_only": session_read_only},
            presentation=_mode_action(active),
        )


def plan_context(context: ToolContext, services: PluginServices) -> ContextContribution | None:
    """Contribute the plan instruction while this owner's overlay is active."""
    _ = context
    return ContextContribution(content=PLAN_MODE_INSTRUCTION) if services.overlay_active() else None
