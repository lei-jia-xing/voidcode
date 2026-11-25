"""Immutable capability generations and the reserved core command/tool specs.

A snapshot captures one admitted revision's descriptors together with the runtime
that serves them. Reserved core commands and tools are declared here so the
command and tool surfaces share one typed source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import jsonschema

from ...sdk.commands import CommandDescriptor
from ...sdk.icons import Icon
from ...sdk.json_values import json_wire_object
from ...sdk.tools import ToolDefinition, ToolEffect
from ..activation import Admission, ExecutionConfig
from ..plugins import PluginRuntime

_CORE_COMMAND_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool], ...] = (
    (
        "run",
        "Run an agent turn with a user prompt",
        {
            "type": "object",
            "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 65536}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "resume",
        "Resume a paused session (approval or interrupted)",
        {
            "type": "object",
            "properties": {
                "approval_request_id": {"type": "string", "minLength": 1},
                "decision": {"enum": ["allow", "deny"]},
            },
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
        True,
    ),
    (
        "show",
        "Show session state, checkpoint, and events",
        {"type": "object", "properties": {"events": {"type": "boolean"}}, "additionalProperties": False},
        (ToolEffect.READ,),
        False,
    ),
    ("status", "Show workspace status", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
    ("help", "Show available commands and documentation", {"type": "object", "additionalProperties": False}, (ToolEffect.READ,), False),
)

#: Tools the core itself dispatches. A plugin can never shadow these names.
_CORE_TOOL_SPECS: tuple[tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], Icon], ...] = (
    (
        "core_reload",
        "Request that the configured plugins be re-admitted as a new capability revision. This only stages the "
        "request: the switch happens after the current tool batch finishes, and it needs user approval first.",
        {
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 500}},
            "additionalProperties": False,
        },
        (ToolEffect.EXECUTE,),
        Icon(glyph="↻"),
    ),
)

#: Reserved core command that activates the configured sources on explicit request.
RELOAD_COMMAND_NAME = "core.reload"
_RELOAD_COMMAND_SPEC: tuple[str, str, Mapping[str, object], tuple[ToolEffect, ...], bool] = (
    RELOAD_COMMAND_NAME,
    "Re-admit the configured plugin sources and switch this workspace to the new capability revision.",
    {"type": "object", "properties": {}, "additionalProperties": False},
    (ToolEffect.EXECUTE, ToolEffect.READ, ToolEffect.WRITE),
    True,
)


def _core_command_descriptors(*, with_reload: bool) -> tuple[CommandDescriptor, ...]:
    """Reserved core command descriptors, captured together with the revision that admits them."""
    specs = (*_CORE_COMMAND_SPECS, _RELOAD_COMMAND_SPEC) if with_reload else _CORE_COMMAND_SPECS
    return tuple(
        CommandDescriptor(
            name=name,
            owner="core",
            description=description,
            argument_schema=dict(schema),
            effects=effects,
            busy_gate=busy_gate,
        )
        for name, description, schema, effects, busy_gate in specs
    )


def _core_tool_definitions() -> dict[str, ToolDefinition]:
    return {
        name: ToolDefinition(name=name, description=description, input_schema=dict(schema), effects=frozenset(effects), icon=icon)
        for name, description, schema, effects, icon in _CORE_TOOL_SPECS
    }


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One immutable capability generation. Every dispatch reads this single pointer."""

    revision_id: str
    config: ExecutionConfig
    admission: Admission | None
    plugin_runtime: PluginRuntime
    native_tools: Mapping[str, ToolDefinition]
    native_validators: Mapping[str, Any]
    native_commands: Mapping[str, CommandDescriptor]
    tools: tuple[ToolDefinition, ...]
    commands: tuple[CommandDescriptor, ...]

    @staticmethod
    def build(
        *,
        revision_id: str,
        config: ExecutionConfig,
        admission: Admission | None,
        plugin_runtime: PluginRuntime,
        with_reload: bool,
    ) -> _Snapshot:
        native_tools = MappingProxyType(_core_tool_definitions()) if with_reload else MappingProxyType({})
        for name in plugin_runtime.tool_definitions:
            if name in native_tools:
                raise ValueError(f"plugin tool {name!r} collides with a reserved tool")
        native_validators_dict = {}
        for name, definition in native_tools.items():
            if definition.input_schema:
                schema_json = json_wire_object(definition.input_schema)
                jsonschema.Draft202012Validator.check_schema(schema_json)
                native_validators_dict[name] = jsonschema.Draft202012Validator(schema_json)
        native_validators = MappingProxyType(native_validators_dict)
        native_commands = MappingProxyType({desc.name: desc for desc in _core_command_descriptors(with_reload=with_reload)})
        return _Snapshot(
            revision_id=revision_id,
            config=config,
            admission=admission,
            plugin_runtime=plugin_runtime,
            native_tools=native_tools,
            native_validators=native_validators,
            native_commands=native_commands,
            tools=(*plugin_runtime.tools, *native_tools.values()),
            commands=(*native_commands.values(), *plugin_runtime.commands),
        )

    def definition_of(self, name: str) -> ToolDefinition | None:
        return self.native_tools.get(name) or self.plugin_runtime.tool_definitions.get(name)

    def validator_of(self, name: str) -> Any | None:
        return self.native_validators.get(name) or self.plugin_runtime.tool_validators.get(name)

    def command_of(self, name: str) -> CommandDescriptor | None:
        return self.native_commands.get(name) or self.plugin_runtime.command_descriptors.get(name)

    def owner_of(self, tool_name: str) -> str:
        return "core" if tool_name in self.native_tools else self.plugin_runtime.owner(tool_name)
