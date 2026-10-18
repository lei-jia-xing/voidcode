"""Kernel configuration schema and strict payload parser for ``voidcode.json``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .json_values import own_json_object

CONFIG_FILE_NAME = "voidcode.json"
CONFIG_SCHEMA_VERSION = 1

_CONFIG_IDENT_REGEX = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class KernelModelConfig:
    model: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("kernel config model.model must be a non-empty string")
        if self.reasoning_effort is not None:
            if not isinstance(self.reasoning_effort, str):
                raise ValueError("kernel config model.reasoning_effort must be a string")
            if self.reasoning_effort not in {"low", "medium", "high", "none"}:
                raise ValueError("kernel config model.reasoning_effort must be one of low/medium/high/none")


@dataclass(frozen=True, slots=True)
class KernelPolicyConfig:
    approval_mode: str = "ask"
    read_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.approval_mode, str) or self.approval_mode not in {"ask", "write", "yolo"}:
            raise ValueError("kernel config policy.approval_mode must be one of ask/write/yolo")
        if type(self.read_only) is not bool:
            raise ValueError("kernel config policy.read_only must be a boolean")


@dataclass(frozen=True, slots=True)
class KernelProviderEntry:
    wire: Literal["openai-chat"] = "openai-chat"
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None

    def __post_init__(self) -> None:
        if self.wire != "openai-chat":
            raise ValueError("provider wire must be 'openai-chat'")
        if self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key):
            raise ValueError("provider api_key must be a non-empty string")
        if self.base_url is not None and (not isinstance(self.base_url, str) or not self.base_url):
            raise ValueError("provider base_url must be a non-empty string")


@dataclass(frozen=True, slots=True)
class KernelPluginEntry:
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
class KernelConfig:
    schema_version: int = CONFIG_SCHEMA_VERSION
    model: KernelModelConfig = field(default_factory=KernelModelConfig)
    policy: KernelPolicyConfig = field(default_factory=KernelPolicyConfig)
    providers: Mapping[str, KernelProviderEntry] = field(default_factory=dict)
    plugins: Mapping[str, KernelPluginEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"kernel config schema_version must be {CONFIG_SCHEMA_VERSION}")
        for name in self.providers:
            if not isinstance(name, str) or not _CONFIG_IDENT_REGEX.fullmatch(name):
                raise ValueError(f"kernel config provider id must match [a-z][a-z0-9_-]*: {name!r}")
        for plugin_id in self.plugins:
            if not isinstance(plugin_id, str) or not _CONFIG_IDENT_REGEX.fullmatch(plugin_id):
                raise ValueError(f"kernel config plugin id must match [a-z][a-z0-9_-]*: {plugin_id!r}")
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
        object.__setattr__(self, "plugins", MappingProxyType(dict(self.plugins)))


def parse_kernel_config(payload: object) -> KernelConfig:
    """Validate an untrusted ``voidcode.json`` payload."""
    if not isinstance(payload, dict):
        raise ValueError("kernel config must be an object")
    unknown = sorted(set(payload) - {"schema_version", "model", "policy", "providers", "plugins"})
    if unknown:
        raise ValueError(f"kernel config has unsupported field: {unknown[0]}")
    schema_version = payload.get("schema_version", CONFIG_SCHEMA_VERSION)
    if type(schema_version) is not int:
        raise ValueError("kernel config schema_version must be an integer")
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"kernel config schema_version must be {CONFIG_SCHEMA_VERSION}")

    raw_model = payload.get("model", {})
    if not isinstance(raw_model, dict):
        raise ValueError("kernel config model must be an object")
    unknown_model = sorted(set(raw_model) - {"model", "reasoning_effort"})
    if unknown_model:
        raise ValueError(f"kernel config model has unsupported field: {unknown_model[0]}")
    model_ref = raw_model.get("model")
    if model_ref is not None and (not isinstance(model_ref, str) or not model_ref.strip()):
        raise ValueError("kernel config model.model must be a non-empty string")
    effort = raw_model.get("reasoning_effort")
    if effort is not None:
        if not isinstance(effort, str):
            raise ValueError("kernel config model.reasoning_effort must be a string")
        if effort not in {"low", "medium", "high", "none"}:
            raise ValueError("kernel config model.reasoning_effort must be one of low/medium/high/none")

    raw_policy = payload.get("policy", {})
    if not isinstance(raw_policy, dict):
        raise ValueError("kernel config policy must be an object")
    unknown_policy = sorted(set(raw_policy) - {"approval_mode", "read_only"})
    if unknown_policy:
        raise ValueError(f"kernel config policy has unsupported field: {unknown_policy[0]}")
    approval_mode = raw_policy.get("approval_mode", "ask")
    if not isinstance(approval_mode, str) or approval_mode not in {"ask", "write", "yolo"}:
        raise ValueError("kernel config policy.approval_mode must be one of ask/write/yolo")
    read_only = raw_policy.get("read_only", False)
    if type(read_only) is not bool:
        raise ValueError("kernel config policy.read_only must be a boolean")

    raw_providers = payload.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise ValueError("kernel config providers must be an object")
    providers: dict[str, KernelProviderEntry] = {}
    for name, entry in raw_providers.items():
        if not isinstance(name, str) or not _CONFIG_IDENT_REGEX.fullmatch(name):
            raise ValueError(f"kernel config provider id must match [a-z][a-z0-9_-]*: {name!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"kernel config providers.{name} must be an object")
        unknown_entry = sorted(set(entry) - {"wire", "api_key", "base_url"})
        if unknown_entry:
            raise ValueError(f"kernel config providers.{name} has unsupported field: {unknown_entry[0]}")
        wire = entry.get("wire", "openai-chat")
        if wire != "openai-chat":
            raise ValueError(f"kernel config providers.{name}.wire must be 'openai-chat'")
        api_key = entry.get("api_key")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError(f"kernel config providers.{name}.api_key must be a non-empty string")
        base_url = entry.get("base_url")
        if base_url is not None and (not isinstance(base_url, str) or not base_url):
            raise ValueError(f"kernel config providers.{name}.base_url must be a non-empty string")
        providers[name] = KernelProviderEntry(wire=wire, api_key=api_key, base_url=base_url)

    raw_plugins = payload.get("plugins", {})
    if not isinstance(raw_plugins, dict):
        raise ValueError("kernel config plugins must be an object")
    plugins: dict[str, KernelPluginEntry] = {}
    for plugin_id, entry in raw_plugins.items():
        if not isinstance(plugin_id, str) or not _CONFIG_IDENT_REGEX.fullmatch(plugin_id):
            raise ValueError(f"kernel config plugin id must match [a-z][a-z0-9_-]*: {plugin_id!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"kernel config plugins.{plugin_id} must be an object")
        unknown_entry = sorted(set(entry) - {"enabled", "config", "path"})
        if unknown_entry:
            raise ValueError(f"kernel config plugins.{plugin_id} has unsupported field: {unknown_entry[0]}")
        enabled = entry.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError(f"kernel config plugins.{plugin_id}.enabled must be a boolean")
        config_raw = entry.get("config", {})
        if not isinstance(config_raw, Mapping):
            raise ValueError(f"kernel config plugins.{plugin_id}.config must be an object")
        config = own_json_object(config_raw)
        path = entry.get("path")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise ValueError(f"kernel config plugins.{plugin_id}.path must be a non-empty string")
        plugins[plugin_id] = KernelPluginEntry(enabled=enabled, config=config, path=path)

    return KernelConfig(
        schema_version=CONFIG_SCHEMA_VERSION,
        model=KernelModelConfig(model=model_ref.strip() if model_ref else None, reasoning_effort=effort),
        policy=KernelPolicyConfig(approval_mode=approval_mode, read_only=read_only),
        providers=MappingProxyType(providers),
        plugins=MappingProxyType(plugins),
    )
