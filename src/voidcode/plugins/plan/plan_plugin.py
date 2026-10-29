"""Plan plugin: plan mode as a plugin-owned restrictive overlay.

Ports the legacy plan-mode mechanism (``collaboration_mode`` in
``src/voidcode/runtime/permissions/policy.py`` plus ``PLAN_MODE_INSTRUCTION`` from
``src/voidcode/runtime/context/window.py``) onto the core's owner-scoped overlay primitive:
- ``plan`` tightens the session with a read-only overlay, records the mode in
  this plugin's own session state and contributes the plan instruction
  to the provider context.
- ``plan.off`` revokes exactly this owner's overlay and stops the contribution. A read-only
  policy frozen by the user (core config or another owner) is never affected: overlays can
  only tighten.
- ``plan.status`` reports the plugin's view of the mode.

The overlay row is owned by this plugin, so while the plugin is active plan mode survives
restarts (the state and the overlay are both durable). On deactivation the core deletes the
overlay row and this plugin's transform parks the mode off, so the parked state stays
truthful and re-enable never silently re-tightens the session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin, StateSchema
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

SCHEMA_VERSION = 1
_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "active"})

PLAN_MODE_INSTRUCTION = (
    "Plan mode is active. You are in a persistent conversational planning state. "
    "Explore, analyze, and formulate a clear, actionable plan. "
    "File modification and command execution that alters state are forbidden; read-only exploration and inspection are allowed. "
    "Deliver a structured, actionable implementation plan in your response. "
    "The user can exit plan mode at any time using /plan off."
)

ENABLED_OUTPUT = (
    "Plan mode enabled. File modification and command execution that alters state are forbidden; "
    "read-only exploration and inspection are allowed. Use /plan off to exit."
)
DISABLED_OUTPUT = "Plan mode disabled."


@dataclass(frozen=True, slots=True)
class PlanState:
    """This plugin's session-scope state: whether plan mode is currently active."""

    active: bool = False

    @classmethod
    def from_payload(cls, raw: Mapping[str, object] | None) -> PlanState:
        if raw is None:
            return cls()
        unknown = sorted(str(key) for key in raw if key not in _STATE_KEYS)
        if unknown:
            raise ValueError(f"plan state field '{unknown[0]}' is not supported")
        missing = sorted(_STATE_KEYS - raw.keys())
        if missing:
            raise ValueError("plan state is missing field(s): " + ", ".join(missing))
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"plan state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        active = raw["active"]
        if not isinstance(active, bool):
            raise ValueError("plan state field 'active' must be a boolean")
        return cls(active=active)

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": SCHEMA_VERSION, "active": self.active}


def _load(services: PluginServices) -> tuple[PlanState, int]:
    state_data, version = services.get_state("session")
    return PlanState.from_payload(state_data), version


class PlanCommand:
    """Enter plan mode: tighten the session and contribute the plan instruction."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="plan",
        description="Enter plan mode: read-only exploration plus a structured plan.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        state, version = _load(services)
        services.set_overlay(tier="read-only")
        if state.active:
            return CommandResult(status="ok", output=ENABLED_OUTPUT, data={"active": True, "changed": False})
        updated = PlanState(active=True)
        services.replace_state(updated.to_payload(), expected_version=version)
        services.stage_event("plan_mode_enabled", {})
        return CommandResult(status="ok", output=ENABLED_OUTPUT, data={"active": True, "changed": True})


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
        state, version = _load(services)
        cleared = services.clear_overlay()
        if not state.active:
            return CommandResult(status="ok", output=DISABLED_OUTPUT, data={"active": False, "changed": cleared})
        services.replace_state(PlanState(active=False).to_payload(), expected_version=version)
        services.stage_event("plan_mode_disabled", {})
        return CommandResult(status="ok", output=DISABLED_OUTPUT, data={"active": False, "changed": True})


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
        state, _version = _load(services)
        return CommandResult(
            status="ok",
            output="Mode: plan" if state.active else "Mode: default",
            data={"active": state.active},
        )


def plan_context(context: ToolContext, services: PluginServices) -> ContextContribution | None:
    """Contribute the plan instruction while plan mode is active."""
    _ = context
    state, _version = _load(services)
    return ContextContribution(content=PLAN_MODE_INSTRUCTION) if state.active else None


def prepare_plan_deactivation(
    state: Mapping[str, object],
    scope: str,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: an active mode parks off; the overlay row is core-owned.

    The activation transaction deletes this owner's overlay, so a kept active flag would
    promise a restriction that no longer exists. After re-enable the user enters plan mode
    again explicitly.
    """
    _ = (scope, scope_key, tip_sequence)
    PlanState.from_payload(state)
    return PlanState(active=False).to_payload()


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(
        commands=(PlanCommand(), PlanOffCommand(), PlanStatusCommand()),
        context=plan_context,
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "active"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "active": {"type": "boolean"},
                },
            },
        ),
        prepare_deactivate=prepare_plan_deactivation,
    )
