"""Plugin manifest contracts: the single declaration plugins submit at admission.

A manifest declares identity, SDK compatibility, capabilities requested, state
schema, and the exact implementation/resource bytes the kernel will admit.
Admission records content hashes; activation refuses any drift.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import jsonschema
from pydantic import TypeAdapter

from .json_values import json_wire_object, own_json_object
from .tools import ToolEffect

MANIFEST_FILE_NAME = "voidcode-plugin.json"
MANIFEST_SCHEMA_VERSION = 1
SDK_PROTOCOL_VERSION = 1

_PLUGIN_ID_REGEX = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")
_MUTATING_EFFECTS = frozenset({ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.SPAWN, ToolEffect.SESSION})


def _validate_non_empty_str(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_posix_rel_path(path: object, *, field_name: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError(f"{field_name} must be a non-empty string")
    if "\\" in path:
        raise ValueError(f"{field_name} must be a relative POSIX path, backslashes not allowed")
    if path.startswith("/"):
        raise ValueError(f"{field_name} must be a relative path, not absolute")
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError(f"{field_name} has invalid path segment: {part!r}")
    return path


def _validate_sha256(sha256: object, *, field_name: str) -> str:
    if not isinstance(sha256, str) or not _SHA256_HEX_REGEX.fullmatch(sha256):
        raise ValueError(f"{field_name} must be 64 lowercase hex characters")
    return sha256


def _validate_entrypoint(entrypoint: object, *, field_name: str) -> str:
    if not isinstance(entrypoint, str) or not entrypoint:
        raise ValueError(f"{field_name} must be a non-empty string")
    if ":" not in entrypoint:
        raise ValueError(f"{field_name} must be in 'module:callable' format")
    parts = entrypoint.split(":")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must contain exactly one colon")
    module_part, callable_part = parts
    if not module_part:
        raise ValueError(f"{field_name} has empty module part")
    module_segments = module_part.split(".")
    for seg in module_segments:
        if not seg.isidentifier():
            raise ValueError(f"{field_name} module segment {seg!r} is not a valid Python identifier")
    if not callable_part.isidentifier():
        raise ValueError(f"{field_name} callable {callable_part!r} is not a valid Python identifier")
    return entrypoint


def validate_json_schema(schema_input: object, *, field_name: str) -> Mapping[str, object]:
    """Validate and snapshot an untrusted JSON schema mapping."""
    if not isinstance(schema_input, Mapping):
        raise ValueError(f"{field_name} must be an object")
    owned = own_json_object(schema_input)
    raw_dict = json_wire_object(owned)
    try:
        jsonschema.Draft202012Validator.check_schema(raw_dict)
    except jsonschema.exceptions.SchemaError as err:
        raise ValueError(f"{field_name} is not a valid JSON schema: {err.message}") from err
    return owned


@dataclass(frozen=True, slots=True)
class PluginFileIdentity:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _validate_posix_rel_path(self.path, field_name="file.path")
        _validate_sha256(self.sha256, field_name="file.sha256")


@dataclass(frozen=True, slots=True)
class PluginProviderDeclaration:
    """One model/provider adapter the plugin exposes."""

    name: str
    description: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, field_name="provider declaration name")
        _validate_non_empty_str(self.description, field_name="provider declaration description")


@dataclass(frozen=True, slots=True)
class PluginToolDeclaration:
    """One tool the plugin registers when its revision activates."""

    name: str
    description: str
    input_schema: Mapping[str, object] = field(default_factory=dict)
    effects: tuple[ToolEffect, ...] = (ToolEffect.READ,)
    replay_policy: str | None = None
    capabilities: tuple[str, ...] = ()
    #: Arguments naming workspace paths, so a path-aware consumer can read them without a name branch.
    path_argument_keys: tuple[str, ...] = ()
    #: Whether this tool may run in a parallel native batch alongside its siblings.
    supports_parallel: bool = False

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, field_name="tool declaration name")
        _validate_non_empty_str(self.description, field_name="tool declaration description")
        owned_schema = validate_json_schema(self.input_schema, field_name=f"tools[{self.name}].input_schema")
        object.__setattr__(self, "input_schema", owned_schema)
        if not isinstance(self.effects, (list, tuple)) or not self.effects:
            raise ValueError("tool declaration effects must be a non-empty sequence")
        validated_effects: list[ToolEffect] = []
        for eff in self.effects:
            if not isinstance(eff, ToolEffect):
                try:
                    eff = ToolEffect(eff)
                except ValueError, KeyError:
                    raise ValueError(f"invalid tool effect: {eff}") from None
            validated_effects.append(eff)
        object.__setattr__(self, "effects", tuple(validated_effects))
        if self.replay_policy is not None and self.replay_policy not in {"safe", "never"}:
            raise ValueError("tool declaration replay_policy must be 'safe' or 'never'")
        if self.replay_policy == "safe" and any(e in _MUTATING_EFFECTS for e in validated_effects):
            raise ValueError("replay_policy 'safe' cannot be used with mutating effects (write/execute/spawn/session)")
        if not isinstance(self.capabilities, (list, tuple)):
            raise ValueError("tool declaration capabilities must be a sequence")
        caps = tuple(_validate_non_empty_str(c, field_name="capability") for c in self.capabilities)
        object.__setattr__(self, "capabilities", caps)
        if not isinstance(self.path_argument_keys, (list, tuple)):
            raise ValueError("tool declaration path_argument_keys must be a sequence")
        keys = tuple(_validate_non_empty_str(key, field_name="path_argument_key") for key in self.path_argument_keys)
        if len(keys) != len(set(keys)):
            raise ValueError(f"tool {self.name} declares duplicate path argument keys")
        object.__setattr__(self, "path_argument_keys", keys)
        if type(self.supports_parallel) is not bool:
            raise ValueError("tool declaration supports_parallel must be a boolean")


@dataclass(frozen=True, slots=True)
class PluginCommandDeclaration:
    """One command the plugin adds to the dynamic command directory."""

    name: str
    description: str
    argument_schema: Mapping[str, object] = field(default_factory=dict)
    effects: tuple[ToolEffect, ...] = ()
    capabilities: tuple[str, ...] = ()
    busy_gate: bool = False
    client_capabilities: tuple[str, ...] = ()
    #: Invoked only by an authentic user ingress, with direct-user authority (no agent claim).
    direct_user: bool = False
    #: Declarative client syntax prefix (e.g. "!"); clients route it, the kernel never parses it.
    client_syntax: str | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, field_name="command declaration name")
        _validate_non_empty_str(self.description, field_name="command declaration description")
        owned_schema = validate_json_schema(self.argument_schema, field_name=f"commands[{self.name}].argument_schema")
        object.__setattr__(self, "argument_schema", owned_schema)
        validated_effects: list[ToolEffect] = []
        for eff in self.effects:
            if not isinstance(eff, ToolEffect):
                try:
                    eff = ToolEffect(eff)
                except ValueError, KeyError:
                    raise ValueError(f"invalid command effect: {eff}") from None
            validated_effects.append(eff)
        object.__setattr__(self, "effects", tuple(validated_effects))
        if not isinstance(self.capabilities, (list, tuple)):
            raise ValueError("command declaration capabilities must be a sequence")
        caps = tuple(_validate_non_empty_str(c, field_name="capability") for c in self.capabilities)
        object.__setattr__(self, "capabilities", caps)
        if type(self.busy_gate) is not bool:
            raise ValueError("command declaration busy_gate must be a boolean")
        if not isinstance(self.client_capabilities, (list, tuple)):
            raise ValueError("command declaration client_capabilities must be a sequence")
        client_caps = tuple(_validate_non_empty_str(c, field_name="client_capability") for c in self.client_capabilities)
        object.__setattr__(self, "client_capabilities", client_caps)
        if type(self.direct_user) is not bool:
            raise ValueError("command declaration direct_user must be a boolean")
        if self.client_syntax is not None:
            if not isinstance(self.client_syntax, str) or not self.client_syntax.strip() or len(self.client_syntax) > 8:
                raise ValueError("command declaration client_syntax must be 1..8 non-space characters")
            if any(character.isspace() for character in self.client_syntax):
                raise ValueError("command declaration client_syntax must not contain whitespace")


@dataclass(frozen=True, slots=True)
class PluginCapabilityRequest:
    """One kernel capability the plugin needs at execution time."""

    name: str
    reason: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, field_name="capability declaration name")
        _validate_non_empty_str(self.reason, field_name="capability declaration reason")


HookKind = Literal["pre_tool", "post_tool", "session_start", "session_end"]


@dataclass(frozen=True, slots=True)
class PluginHookDeclaration:
    """One declared hook: strongly typed kind, explicit tool match, declared mutation stance."""

    kind: HookKind
    match: tuple[str, ...] = ("*",)
    mutating: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("pre_tool", "post_tool", "session_start", "session_end"):
            raise ValueError(f"unsupported hook kind: {self.kind!r}")
        if not self.match or any(not isinstance(pattern, str) or not pattern for pattern in self.match):
            raise ValueError("hook match must list at least one non-empty tool name or '*'")
        if not isinstance(self.mutating, bool):
            raise ValueError("hook mutating must be a boolean")


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    sdk_protocol: int = SDK_PROTOCOL_VERSION
    schema_version: int = MANIFEST_SCHEMA_VERSION
    entrypoint: str = ""
    files: tuple[PluginFileIdentity, ...] = ()
    tools: tuple[PluginToolDeclaration, ...] = ()
    commands: tuple[PluginCommandDeclaration, ...] = ()
    capabilities: tuple[PluginCapabilityRequest, ...] = ()
    hooks: tuple[PluginHookDeclaration, ...] = ()
    hooks: tuple[PluginHookDeclaration, ...] = ()
    providers: tuple[PluginProviderDeclaration, ...] = ()
    depends: tuple[str, ...] = ()
    state_schema_version: int = 1
    state_schema: Mapping[str, object] | None = None
    config_schema: Mapping[str, object] = field(default_factory=dict)
    context: bool = False
    observed_facts: tuple[Literal["usage", "invocation_finished", "run_finished"], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.plugin_id, str) or not _PLUGIN_ID_REGEX.fullmatch(self.plugin_id):
            raise ValueError(f"plugin manifest plugin_id must match [a-z][a-z0-9_-]*: {self.plugin_id!r}")
        if not self.version or not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("plugin manifest requires a non-empty version")
        if type(self.schema_version) is not int or self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"plugin manifest schema_version must be {MANIFEST_SCHEMA_VERSION}")
        if type(self.sdk_protocol) is not int or self.sdk_protocol != SDK_PROTOCOL_VERSION:
            raise ValueError(f"plugin manifest sdk_protocol must be {SDK_PROTOCOL_VERSION}")
        _validate_entrypoint(self.entrypoint, field_name="entrypoint")
        if type(self.state_schema_version) is not int or self.state_schema_version < 1:
            raise ValueError("plugin manifest state_schema_version must be >= 1")

        if type(self.context) is not bool:
            raise ValueError("plugin manifest context must be a boolean")
        if not isinstance(self.observed_facts, (list, tuple)):
            raise ValueError("plugin manifest observed_facts must be a sequence")
        valid_facts = {"usage", "invocation_finished", "run_finished"}
        for f in self.observed_facts:
            if f not in valid_facts:
                raise ValueError(f"invalid observed_fact: {f}")
        object.__setattr__(self, "observed_facts", tuple(self.observed_facts))

        file_paths = [f.path for f in self.files]
        if len(file_paths) != len(set(file_paths)):
            raise ValueError("plugin manifest declares duplicate file paths")

        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("plugin manifest declares duplicate tool names")

        command_names = [command.name for command in self.commands]
        if len(command_names) != len(set(command_names)):
            raise ValueError("plugin manifest declares duplicate command names")

        capability_names = [capability.name for capability in self.capabilities]
        if len(capability_names) != len(set(capability_names)):
            raise ValueError("plugin manifest declares duplicate capabilities")

        provider_names = [provider.name for provider in self.providers]
        if len(provider_names) != len(set(provider_names)):
            raise ValueError("plugin manifest declares duplicate provider names")

        if len(self.depends) != len(set(self.depends)):
            raise ValueError("plugin manifest declares duplicate dependencies")
        for dep in self.depends:
            if not isinstance(dep, str) or not dep.strip():
                raise ValueError("plugin manifest dependencies must be non-empty strings")
        if self.plugin_id in self.depends:
            raise ValueError("plugin manifest must not depend on itself")

        declared_caps = set(capability_names)
        for tool in self.tools:
            for cap in tool.capabilities:
                if cap not in declared_caps:
                    raise ValueError(f"tool {tool.name} requires undeclared capability: {cap}")
        for command in self.commands:
            for cap in command.capabilities:
                if cap not in declared_caps:
                    raise ValueError(f"command {command.name} requires undeclared capability: {cap}")

        owned_config_schema = validate_json_schema(self.config_schema, field_name="config_schema")
        object.__setattr__(self, "config_schema", owned_config_schema)
        if self.state_schema is not None:
            object.__setattr__(self, "state_schema", validate_json_schema(self.state_schema, field_name="state_schema"))

    def as_payload(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "sdk_protocol": self.sdk_protocol,
            "schema_version": self.schema_version,
            "entrypoint": self.entrypoint,
            "files": [{"path": item.path, "sha256": item.sha256} for item in self.files],
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": json_wire_object(tool.input_schema),
                    "effects": [effect.value for effect in tool.effects],
                    "replay_policy": tool.replay_policy,
                    "capabilities": list(tool.capabilities),
                    "path_argument_keys": list(tool.path_argument_keys),
                    "supports_parallel": tool.supports_parallel,
                }
                for tool in self.tools
            ],
            "commands": [
                {
                    "name": command.name,
                    "description": command.description,
                    "argument_schema": json_wire_object(command.argument_schema),
                    "effects": [effect.value for effect in command.effects],
                    "capabilities": list(command.capabilities),
                    "busy_gate": command.busy_gate,
                    "client_capabilities": list(command.client_capabilities),
                    "direct_user": command.direct_user,
                    "client_syntax": command.client_syntax,
                }
                for command in self.commands
            ],
            "capabilities": [{"name": item.name, "reason": item.reason} for item in self.capabilities],
            "hooks": [{"kind": hook.kind, "match": list(hook.match), "mutating": hook.mutating} for hook in self.hooks],
            "providers": [{"name": item.name, "description": item.description} for item in self.providers],
            "depends": list(self.depends),
            "state_schema_version": self.state_schema_version,
            "state_schema": None if self.state_schema is None else json_wire_object(self.state_schema),
            "config_schema": json_wire_object(self.config_schema),
            "context": self.context,
            "observed_facts": list(self.observed_facts),
        }


_MANIFEST_ADAPTER = TypeAdapter(PluginManifest)


def parse_plugin_manifest(payload: object) -> PluginManifest:
    """Validate JSON types and unknown fields once, including nested records."""
    if not isinstance(payload, dict):
        raise ValueError("plugin manifest must be an object")
    wire = json_wire_object(own_json_object(payload))
    return _MANIFEST_ADAPTER.validate_json(json.dumps(wire, allow_nan=False), strict=True, extra="forbid")
