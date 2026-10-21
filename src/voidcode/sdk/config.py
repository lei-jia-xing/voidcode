"""Core configuration schema and strict payload parser for ``voidcode.json``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .json_values import own_json_object

CONFIG_FILE_NAME = "voidcode.json"
CONFIG_SCHEMA_VERSION = 1

_CONFIG_IDENT_REGEX = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class CoreModelConfig:
    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("core config model.model must be a non-empty string")
        if self.reasoning_effort is not None:
            if not isinstance(self.reasoning_effort, str):
                raise ValueError("core config model.reasoning_effort must be a string")
            if self.reasoning_effort not in {"low", "medium", "high", "none"}:
                raise ValueError("core config model.reasoning_effort must be one of low/medium/high/none")


@dataclass(frozen=True, slots=True)
class CorePolicyConfig:
    approval_mode: str = "ask"
    read_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.approval_mode, str) or self.approval_mode not in {"ask", "write", "yolo"}:
            raise ValueError("core config policy.approval_mode must be one of ask/write/yolo")
        if type(self.read_only) is not bool:
            raise ValueError("core config policy.read_only must be a boolean")


@dataclass(frozen=True, slots=True)
class CoreProviderEntry:
    """Endpoint configuration for one provider name registered by a plugin.

    The configured name IS the selection: the core passes it to the runtime unchanged and never
    knows a wire protocol, vendor, or adapter identity.
    """

    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    #: Every other configured field, passed to the declaring plugin verbatim. The core does
    #: not know a vendor, a header name, or a query parameter, so it neither validates nor
    #: interprets these: they are the provider's own credentials vocabulary, and a repr must
    #: never expose a credential the core cannot recognize.
    extra: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key):
            raise ValueError("provider api_key must be a non-empty string")
        if self.base_url is not None and (not isinstance(self.base_url, str) or not self.base_url):
            raise ValueError("provider base_url must be a non-empty string")
        if not isinstance(self.extra, Mapping):
            raise ValueError("provider extra fields must be an object")
        for name in self.extra:
            if not isinstance(name, str) or not name:
                raise ValueError("provider extra field names must be non-empty strings")
        if set(self.extra) & {"api_key", "base_url"}:
            raise ValueError("provider extra fields must not repeat api_key or base_url")
        object.__setattr__(self, "extra", own_json_object(self.extra))


@dataclass(frozen=True, slots=True)
class CorePluginEntry:
    enabled: bool = True
    config: Mapping[str, object] = field(default_factory=dict)
    path: str | None = None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("plugin entry enabled must be a boolean")
        if not isinstance(self.config, Mapping):
            raise ValueError("plugin entry config must be an object")
        object.__setattr__(self, "config", own_json_object(self.config))
        if self.path is not None and (not isinstance(self.path, str) or not self.path.strip()):
            raise ValueError("plugin entry path must be a non-empty string")


@dataclass(frozen=True, slots=True)
class CoreConfig:
    schema_version: int = CONFIG_SCHEMA_VERSION
    model: CoreModelConfig = field(default_factory=CoreModelConfig)
    policy: CorePolicyConfig = field(default_factory=CorePolicyConfig)
    providers: Mapping[str, CoreProviderEntry] = field(default_factory=dict)
    plugins: Mapping[str, CorePluginEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"core config schema_version must be {CONFIG_SCHEMA_VERSION}")
        for name in self.providers:
            if not isinstance(name, str) or not _CONFIG_IDENT_REGEX.fullmatch(name):
                raise ValueError(f"core config provider id must match [a-z][a-z0-9_-]*: {name!r}")
        for plugin_id in self.plugins:
            if not isinstance(plugin_id, str) or not _CONFIG_IDENT_REGEX.fullmatch(plugin_id):
                raise ValueError(f"core config plugin id must match [a-z][a-z0-9_-]*: {plugin_id!r}")
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
        object.__setattr__(self, "plugins", MappingProxyType(dict(self.plugins)))


def parse_core_config(payload: object) -> CoreConfig:
    """Validate an untrusted ``voidcode.json`` payload."""
    if not isinstance(payload, dict):
        raise ValueError("core config must be an object")
    unknown = sorted(set(payload) - {"schema_version", "model", "policy", "providers", "plugins"})
    if unknown:
        raise ValueError(f"core config has unsupported field: {unknown[0]}")
    schema_version = payload.get("schema_version", CONFIG_SCHEMA_VERSION)
    if type(schema_version) is not int:
        raise ValueError("core config schema_version must be an integer")
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"core config schema_version must be {CONFIG_SCHEMA_VERSION}")

    raw_model = payload.get("model", {})
    if not isinstance(raw_model, dict):
        raise ValueError("core config model must be an object")
    unknown_model = sorted(set(raw_model) - {"model", "reasoning_effort"})
    if unknown_model:
        raise ValueError(f"core config model has unsupported field: {unknown_model[0]}")
    model_ref = raw_model.get("model")
    if model_ref is not None and (not isinstance(model_ref, str) or not model_ref.strip()):
        raise ValueError("core config model.model must be a non-empty string")
    effort = raw_model.get("reasoning_effort")
    if effort is not None:
        if not isinstance(effort, str):
            raise ValueError("core config model.reasoning_effort must be a string")
        if effort not in {"low", "medium", "high", "none"}:
            raise ValueError("core config model.reasoning_effort must be one of low/medium/high/none")

    raw_policy = payload.get("policy", {})
    if not isinstance(raw_policy, dict):
        raise ValueError("core config policy must be an object")
    unknown_policy = sorted(set(raw_policy) - {"approval_mode", "read_only"})
    if unknown_policy:
        raise ValueError(f"core config policy has unsupported field: {unknown_policy[0]}")
    approval_mode = raw_policy.get("approval_mode", "ask")
    if not isinstance(approval_mode, str) or approval_mode not in {"ask", "write", "yolo"}:
        raise ValueError("core config policy.approval_mode must be one of ask/write/yolo")
    read_only = raw_policy.get("read_only", False)
    if type(read_only) is not bool:
        raise ValueError("core config policy.read_only must be a boolean")

    raw_providers = payload.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise ValueError("core config providers must be an object")
    providers: dict[str, CoreProviderEntry] = {}
    for name, entry in raw_providers.items():
        if not isinstance(name, str) or not _CONFIG_IDENT_REGEX.fullmatch(name):
            raise ValueError(f"core config provider id must match [a-z][a-z0-9_-]*: {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"core config providers.{name} must be an object")
        api_key = entry.get("api_key")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError(f"core config providers.{name}.api_key must be a non-empty string")
        base_url = entry.get("base_url")
        if base_url is not None and (not isinstance(base_url, str) or not base_url):
            raise ValueError(f"core config providers.{name}.base_url must be a non-empty string")
        # Vendor fields are opaque: everything that is not the typed api_key/base_url travels
        # to the declaring plugin exactly as configured (auth headers, account ids, regions).
        extra = {key: value for key, value in entry.items() if key not in {"api_key", "base_url"}}
        providers[name] = CoreProviderEntry(api_key=api_key, base_url=base_url, extra=extra)

    raw_plugins = payload.get("plugins", {})
    if not isinstance(raw_plugins, dict):
        raise ValueError("core config plugins must be an object")
    plugins: dict[str, CorePluginEntry] = {}
    for plugin_id, entry in raw_plugins.items():
        if not isinstance(plugin_id, str) or not _CONFIG_IDENT_REGEX.fullmatch(plugin_id):
            raise ValueError(f"core config plugin id must match [a-z][a-z0-9_-]*: {plugin_id!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"core config plugins.{plugin_id} must be an object")
        unknown_entry = sorted(set(entry) - {"enabled", "config", "path"})
        if unknown_entry:
            raise ValueError(f"core config plugins.{plugin_id} has unsupported field: {unknown_entry[0]}")
        enabled = entry.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError(f"core config plugins.{plugin_id}.enabled must be a boolean")
        config_raw = entry.get("config", {})
        if not isinstance(config_raw, Mapping):
            raise ValueError(f"core config plugins.{plugin_id}.config must be an object")
        config = own_json_object(config_raw)
        path = entry.get("path")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ValueError(f"core config plugins.{plugin_id}.path must be a non-empty string")
        plugins[plugin_id] = CorePluginEntry(enabled=enabled, config=config, path=path)

    return CoreConfig(
        schema_version=CONFIG_SCHEMA_VERSION,
        model=CoreModelConfig(model=model_ref.strip() if model_ref else None, reasoning_effort=effort),
        policy=CorePolicyConfig(approval_mode=approval_mode, read_only=read_only),
        providers=MappingProxyType(providers),
        plugins=MappingProxyType(plugins),
    )
