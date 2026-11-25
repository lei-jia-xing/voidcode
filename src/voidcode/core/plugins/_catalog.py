"""Plugin loading, admission checks, and the immutable tool/provider/command catalog."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import sys
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, get_args

import jsonschema

from voidcode.sdk.commands import CommandDescriptor, CommandInput
from voidcode.sdk.hooks import (
    ObservedFactKind,
)
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import (
    ModelContextLimits,
    ModelLimitsProvider,
    StreamableTurnProvider,
)
from voidcode.sdk.tools import (
    ToolDefinition,
)

from ._base import RESERVED_COMMAND_NAMES, RESERVED_COMMAND_PREFIXES, RESERVED_TOOL_NAMES
from ._contracts import PluginSource
from ._state import PluginState


class PluginCatalog(PluginState):
    """Plugin loading, admission checks, and the immutable tool/provider/command catalog."""

    def _load_plugin(self, source: PluginSource) -> None:
        plugin_id = source.plugin_id
        root = source.root.resolve()
        entry_module = root / "__init__.py"
        if not entry_module.is_file():
            raise ValueError(f"Plugin {plugin_id} root is not a package (no __init__.py): {root}")
        # Flat unique module name per runtime + id: no synthetic parent package to fabricate,
        # and "-" is encoded so distinct ids can never collide after normalization.
        safe_id = plugin_id.replace("_", "__").replace("-", "_")
        mod_name = f"{self._namespace}__{safe_id}"
        spec = importlib.util.spec_from_file_location(
            mod_name,
            str(entry_module),
            submodule_search_locations=[str(root)],
        )
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            raise ValueError(f"Failed to create module spec for plugin {plugin_id}: {entry_module}")
        loader = spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        self._modules.append(mod_name)
        try:
            # Compile from current source bytes: pyc validation is second-granularity,
            # so a same-second source rewrite would otherwise serve stale code.
            source_bytes = loader.get_data(str(entry_module))
            code = loader.source_to_code(source_bytes, str(entry_module))
            exec(code, mod.__dict__)
        finally:
            for name in list(sys.modules):
                if name.startswith(f"{mod_name}.") and name not in self._modules:
                    self._modules.append(name)

        factory = getattr(mod, "create_plugin", None)
        if factory is None or not callable(factory):
            raise ValueError(f"Plugin {plugin_id} does not export create_plugin")
        try:
            plugin_instance = factory(json_wire_object(source.config))
        except Exception as exc:
            raise ValueError(f"Plugin {plugin_id} factory failed: {exc}") from exc
        if not isinstance(plugin_instance, Plugin):
            raise ValueError(f"Plugin {plugin_id} factory returned {type(plugin_instance).__name__}, expected Plugin")
        # Recorded before registration validation: a duplicate/reserved name must still close.
        self._plugins.append(plugin_instance)
        self._plugin_ids.append(plugin_id)
        self._plugin_states[plugin_id] = plugin_instance.state

        seen_tools: set[str] = set()
        for tool in plugin_instance.tools:
            name = tool.definition.name
            if name in RESERVED_TOOL_NAMES:
                raise ValueError(f"Tool '{name}' in plugin {plugin_id} is a reserved tool name")
            if name in seen_tools or name in self._tools:
                raise ValueError(f"Plugin {plugin_id} registers duplicate tool name: {name}")
            seen_tools.add(name)
            self._tools[name] = (tool, plugin_id)

        seen_commands: set[str] = set()
        for command in plugin_instance.commands:
            name = command.definition.name
            if name in RESERVED_COMMAND_NAMES or any(name.startswith(prefix) for prefix in RESERVED_COMMAND_PREFIXES):
                raise ValueError(f"Command '{name}' in plugin {plugin_id} is a reserved command name or prefix")
            if name in seen_commands or name in self._commands:
                raise ValueError(f"Plugin {plugin_id} registers duplicate command name: {name}")
            if isinstance(command, CommandInput):
                _check_arity(command.prepare_input, 2, f"Plugin {plugin_id} command {name} input preparation")
            seen_commands.add(name)
            self._commands[name] = (command, plugin_id)

        seen_providers: set[str] = set()
        for provider in plugin_instance.providers:
            name = provider.name
            if name in seen_providers or name in self._providers:
                raise ValueError(f"Plugin {plugin_id} registers duplicate provider name: {name}")
            seen_providers.add(name)
            self._providers[name] = (provider, plugin_id)

        seen_views: set[str] = set()
        for view in plugin_instance.views:
            name = view.name
            if name in seen_views or name in self._views:
                raise ValueError(f"Plugin {plugin_id} registers duplicate view name: {name}")
            seen_views.add(name)
            self._views[name] = (view, plugin_id)

        if plugin_instance.context is not None:
            _check_arity(plugin_instance.context, 2, f"Plugin {plugin_id} context callback")
            self._context_callbacks[plugin_id] = plugin_instance.context
        if plugin_instance.observer is not None:
            _check_arity(plugin_instance.observer, 3, f"Plugin {plugin_id} observer callback")
            self._observers.append((plugin_instance.observer, plugin_instance.observed_facts, plugin_id))
        if plugin_instance.background_action is not None:
            _check_arity(plugin_instance.background_action, 1, f"Plugin {plugin_id} background_action callback")
            self._background_actions[plugin_id] = plugin_instance.background_action
        if plugin_instance.post_tool is not None:
            _check_arity(plugin_instance.post_tool, 3, f"Plugin {plugin_id} post_tool callback")
            self._post_tool_hooks.append((plugin_instance.post_tool, plugin_instance.post_tool_match, plugin_instance.post_tool_mutating, plugin_id))
        for kind, attribute in (("session_start", "session_start"), ("session_end", "session_end")):
            callback = getattr(plugin_instance, attribute)
            if callback is not None:
                _check_arity(callback, 3, f"Plugin {plugin_id} {kind} callback")
                self._lifecycle_hooks[f"{plugin_id}\0{kind}"] = (callback, plugin_id)
        if plugin_instance.pre_tool is not None:
            _check_arity(plugin_instance.pre_tool, 3, f"Plugin {plugin_id} pre_tool callback")
            self._pre_tool_hooks.append((plugin_instance.pre_tool, plugin_instance.pre_tool_match, plugin_instance.pre_tool_mutating, plugin_id))
        if plugin_instance.prepare_deactivate is not None:
            _check_arity(plugin_instance.prepare_deactivate, 4, f"Plugin {plugin_id} prepare_deactivate callback")
            self._state_deactivators[plugin_id] = plugin_instance.prepare_deactivate
        for query in plugin_instance.queries:
            if query.name in self._queries:
                raise ValueError(f"Duplicate query name registered across plugins: {query.name}")
            _check_arity(query.handler, 2, f"Plugin {plugin_id} query {query.name} handler")
            self._queries[query.name] = (query.handler, plugin_id)
        for reader in plugin_instance.resource_readers:
            existing = self._resource_readers.get(reader.scheme)
            if existing is not None:
                raise ValueError(f"Duplicate resource scheme {reader.scheme!r} registered by {plugin_id} and {existing[1]}")
            _check_arity(reader.handler, 3, f"Plugin {plugin_id} resource reader {reader.scheme} handler")
            self._resource_readers[reader.scheme] = (reader.handler, plugin_id)

    def _build_catalog(self) -> None:
        self._tool_defs = tuple(tool.definition for tool, _ in self._tools.values())
        self._tool_map = {definition.name: definition for definition in self._tool_defs}
        self._provider_names = tuple(self._providers)
        self._tool_owners = {name: plugin_id for name, (_, plugin_id) in self._tools.items()}
        self._validators = {}
        for name, definition in self._tool_map.items():
            if definition.input_schema:
                schema_json = json_wire_object(definition.input_schema)
                try:
                    jsonschema.Draft202012Validator.check_schema(schema_json)
                except jsonschema.exceptions.SchemaError as err:
                    plugin_id = self._tool_owners.get(name, "unknown")
                    raise ValueError(f"Plugin {plugin_id} tool {name} has an invalid input schema: {err.message}") from err
                self._validators[name] = jsonschema.Draft202012Validator(schema_json)
            if definition.output_schema:
                output_json = json_wire_object(definition.output_schema)
                try:
                    jsonschema.Draft202012Validator.check_schema(output_json)
                except jsonschema.exceptions.SchemaError as err:
                    plugin_id = self._tool_owners.get(name, "unknown")
                    raise ValueError(f"Plugin {plugin_id} tool {name} has an invalid output schema: {err.message}") from err
        self._command_descs = tuple(
            CommandDescriptor(
                name=command.definition.name,
                owner=plugin_id,
                description=command.definition.description,
                argument_schema=command.definition.argument_schema,
                effects=tuple(sorted(command.definition.effects, key=lambda effect: effect.value)),
                busy_gate=command.definition.busy_gate,
                client_syntax=command.definition.client_syntax,
                has_input=isinstance(command, CommandInput),
                slash_visible=command.definition.slash_visible,
                text_argument=command.definition.text_argument,
                default_shortcut=command.definition.default_shortcut,
                usage=command.definition.usage,
                examples=command.definition.examples,
                group=command.definition.group,
            )
            for command, plugin_id in self._commands.values()
        )
        self._command_map = {desc.name: desc for desc in self._command_descs}
        self._command_map_view: Mapping[str, CommandDescriptor] = MappingProxyType(self._command_map)
        self._command_validators = {}
        self._command_prep_validators = {}
        for desc in self._command_descs:
            if desc.text_argument is not None:
                properties = desc.argument_schema.get("properties", {})
                prop = properties.get(desc.text_argument) if isinstance(properties, Mapping) else None
                if not isinstance(prop, Mapping) or prop.get("type") != "string":
                    raise ValueError(f"Command {desc.name} text_argument must name a declared string property")
            if desc.argument_schema:
                schema_json = json_wire_object(desc.argument_schema)
                try:
                    jsonschema.Draft202012Validator.check_schema(schema_json)
                except jsonschema.exceptions.SchemaError as err:
                    raise ValueError(f"Plugin {desc.owner} command {desc.name} has an invalid argument schema: {err.message}") from err
                self._command_validators[desc.name] = jsonschema.Draft202012Validator(schema_json)
                prep_schema = dict(schema_json)
                # Preparation validates supplied keys/types only: invocation-required
                # fields stay missing until the real submit, so bare forms keep opening.
                prep_schema.pop("required", None)
                self._command_prep_validators[desc.name] = jsonschema.Draft202012Validator(prep_schema)
            else:
                self._command_validators[desc.name] = None
                self._command_prep_validators[desc.name] = None
        self._view_names = tuple(self._views)
        self._view_owners = {name: plugin_id for name, (_, plugin_id) in self._views.items()}
        self._tool_map_view: Mapping[str, ToolDefinition] = MappingProxyType(self._tool_map)
        self._validators_view: Mapping[str, Any] = MappingProxyType(self._validators)
        self._command_validators_view: Mapping[str, Any] = MappingProxyType(self._command_validators)
        self._context_owners = tuple(self._context_callbacks)
        self._observer_owners: dict[ObservedFactKind, tuple[str, ...]] = {
            fact_type: tuple(pid for _, obs_facts, pid in self._observers if fact_type in obs_facts) for fact_type in get_args(ObservedFactKind)
        }

    @property
    def tools(self) -> tuple[ToolDefinition, ...]:
        return self._tool_defs

    @property
    def providers(self) -> tuple[str, ...]:
        return self._provider_names

    def provider_supports_streaming(self, provider: str) -> bool:
        """Whether the registered provider answers stream_turn; unknown names never do."""
        entry = self._providers.get(provider)
        return entry is not None and isinstance(entry[0], StreamableTurnProvider)

    def provider_model_limits(self, provider: str, model_name: str) -> ModelContextLimits | None:
        """Advertised context limits for one model; ``None`` when unknown or unadvertised.

        Limits are advisory input to projection sizing only: an unregistered
        provider, a provider without the probe, or a failing probe all degrade
        to unknown rather than breaking dispatch assembly.
        """
        if not isinstance(model_name, str) or not model_name.strip():
            return None
        entry = self._providers.get(provider)
        if entry is None or not isinstance(entry[0], ModelLimitsProvider):
            return None
        try:
            limits = entry[0].model_limits(model_name)
        except Exception:
            return None
        return limits if isinstance(limits, ModelContextLimits) or limits is None else None

    @property
    def commands(self) -> tuple[CommandDescriptor, ...]:
        return self._command_descs

    @property
    def tool_definitions(self) -> Mapping[str, ToolDefinition]:
        return self._tool_map_view

    @property
    def tool_validators(self) -> Mapping[str, Any]:
        return self._validators_view

    @property
    def command_descriptors(self) -> Mapping[str, CommandDescriptor]:
        return self._command_map_view

    @property
    def command_validators(self) -> Mapping[str, Any]:
        return self._command_validators_view

    def command_retries_state_conflict(self, name: str) -> bool:
        """Whether one registered command opted into bounded state-conflict re-runs."""
        entry = self._commands.get(name)
        return False if entry is None else entry[0].definition.retry_state_conflict

    @property
    def views(self) -> tuple[str, ...]:
        return self._view_names

    def owner(self, tool_name: str) -> str:
        if tool_name not in self._tool_owners:
            raise KeyError(f"Tool {tool_name} not found in catalog")
        return self._tool_owners[tool_name]

    def view_owner(self, view_name: str) -> str:
        if view_name not in self._view_owners:
            raise KeyError(f"View {view_name} not found in catalog")
        return self._view_owners[view_name]

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        """The plugin ids this runtime actually loaded, in admission order."""
        return tuple(self._plugin_ids)

    @property
    def observer_owners(self) -> Mapping[ObservedFactKind, tuple[str, ...]]:
        """Fact kind -> owners that registered it in this generation."""
        return dict(self._observer_owners)

    @property
    def lifecycle_hooks(self) -> tuple[tuple[str, str], ...]:
        """Declared lifecycle hooks: (owner, kind) pairs, kinds session_start/session_end."""
        return tuple((plugin_id, kind) for key, (_, plugin_id) in self._lifecycle_hooks.items() for kind in (key.split("\0", 1)[1],))

    @property
    def pre_tool_hooks(self) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        """Declared pre-tool gates: (owner, tool match patterns, mutating)."""
        return tuple((plugin_id, match, mutating) for _, match, mutating, plugin_id in self._pre_tool_hooks)

    def background_driver(self, owner: str) -> Any | None:
        """The background driver one owner registered, or None when it declares none."""
        return self._background_actions.get(owner)

    @property
    def post_tool_hooks(self) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        """Declared post-tool hooks: (owner, tool match patterns, mutating)."""
        return tuple((plugin_id, match, mutating) for _, match, mutating, plugin_id in self._post_tool_hooks)


def _check_arity(fn: Any, expected_arity: int, name: str) -> None:
    try:
        sig = inspect.signature(fn)
        sig.bind(*([None] * expected_arity))
    except Exception as exc:
        raise ValueError(f"{name} must accept {expected_arity} positional arguments: {exc}") from exc
