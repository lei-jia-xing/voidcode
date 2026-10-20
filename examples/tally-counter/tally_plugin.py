"""Tally counter: an ordinary third-party plugin over shared per-session state.

One model tool (``tally_add``) and one user command (``tally``) mutate the same
session-scope state document; one finite view (``tally``) renders it. State is
validated strictly on every read (unknown keys refused), and every mutation
goes through optimistic ``get_state``/``replace_state`` versioning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin, command_definition, tool_definition
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolFailure, ToolResult, ToolSuccess
from voidcode.sdk.views import PluginViewDescriptor

SCHEMA_VERSION = 1
_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "total", "updates"})


@dataclass(frozen=True, slots=True)
class TallyState:
    total: int = 0
    updates: int = 0

    @classmethod
    def from_payload(cls, raw: Mapping[str, object] | None) -> TallyState:
        if raw is None:
            return cls()
        unknown = sorted(str(key) for key in raw if key not in _STATE_KEYS)
        if unknown:
            raise ValueError(f"tally state field {unknown[0]!r} is not supported")
        missing = sorted(_STATE_KEYS - raw.keys())
        if missing:
            raise ValueError("tally state is missing field(s): " + ", ".join(missing))
        if raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"tally state schema_version must be {SCHEMA_VERSION}, got {raw['schema_version']!r}")
        total = raw["total"]
        if not isinstance(total, int) or isinstance(total, bool):
            raise ValueError("tally state field 'total' must be an integer")
        updates = raw["updates"]
        if not isinstance(updates, int) or isinstance(updates, bool) or updates < 0:
            raise ValueError("tally state field 'updates' must be a non-negative integer")
        return cls(total=total, updates=updates)

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": SCHEMA_VERSION, "total": self.total, "updates": self.updates}


def _load(services: PluginServices) -> tuple[TallyState, int]:
    state_data, version = services.get_state("session")
    return TallyState.from_payload(state_data), version


def _delta(arguments: Mapping[str, object]) -> int:
    # The kernel already enforced the admitted schema (an integer-typed delta)
    # before dispatch; JSON Schema's `integer` also accepts integral floats
    # (3.0), which JSON cannot distinguish from 3 on the wire. Normalize that
    # representation here so committed state stays a true integer.
    value = arguments["delta"]
    if isinstance(value, float):
        value = int(value)
    return cast(int, value)


def _apply(state: TallyState, delta: int) -> TallyState:
    return TallyState(total=state.total + delta, updates=state.updates + 1)


class TallyAddTool:
    """Add a delta to the session tally (model tool, SESSION effect)."""

    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        _ = context
        delta = _delta(call.arguments)
        try:
            state, version = _load(services)
        except ValueError as exc:
            return ToolFailure(tool_name=self.definition.name, error=str(exc), output=TextOutput(str(exc)))
        updated = _apply(state, delta)
        services.replace_state(updated.to_payload(), expected_version=version)
        text = f"tally is now {updated.total} after {updated.updates} update(s)"
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text))


class TallyCommand:
    """Add a delta to the session tally (user command, SESSION effect)."""

    def __init__(self, definition: CommandDefinition) -> None:
        self.definition = definition

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        delta = _delta(invocation.arguments)
        try:
            state, version = _load(services)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        updated = _apply(state, delta)
        services.replace_state(updated.to_payload(), expected_version=version)
        return CommandResult(
            status="ok",
            output=f"tally is now {updated.total} after {updated.updates} update(s)",
            data={"total": updated.total, "updates": updated.updates},
        )


class TallyView:
    name: str = "tally"

    def render(
        self,
        session_id: str,
        services: PluginServices,
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> PluginViewDescriptor:
        _ = (session_id, client_capabilities)
        state, _version = _load(services)
        return PluginViewDescriptor(
            owner="tally-counter",
            name=self.name,
            title="Tally",
            view_type="status",
            data={"label": "Tally", "value": f"{state.total} ({state.updates} update(s))"},
        )


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    """Plugin entrypoint registered in voidcode-plugin.json."""
    import json

    from voidcode.sdk.manifest import parse_plugin_manifest

    manifest = parse_plugin_manifest(json.loads((Path(__file__).parent / "voidcode-plugin.json").read_text(encoding="utf-8")))
    (tool_decl,) = manifest.tools
    (command_decl,) = manifest.commands
    # Registered definitions MUST equal the admitted declarations exactly; deriving
    # both from the manifest (the pattern exec/lsp/session_info use) makes drift
    # impossible.
    return Plugin(
        tools=(TallyAddTool(tool_definition(tool_decl)),),
        commands=(TallyCommand(command_definition(command_decl)),),
        views=(TallyView(),),
    )
