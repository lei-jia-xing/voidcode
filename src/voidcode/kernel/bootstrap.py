"""Product wiring: source trust, new configuration, and ordinary plugin roots."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from graphlib import TopologicalSorter
from importlib.metadata import version
from pathlib import Path

from ..sdk.config import CONFIG_FILE_NAME, KernelConfig, KernelPluginEntry, parse_kernel_config
from ..sdk.plugin import HostPlugin
from .activation import admit_plugin
from .engine import Kernel
from .storage.store import canonical_json


def state_directory(workspace: Path) -> Path:
    identity = hashlib.sha256(os.fsencode(workspace.resolve(strict=True))).hexdigest()
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return root / "voidcode" / "kernel-v1" / identity


def load_config(workspace: Path) -> KernelConfig:
    path = workspace / CONFIG_FILE_NAME
    if not path.is_file():
        if (workspace / ".voidcode.json").exists():
            raise ValueError("legacy .voidcode.json is not supported; create voidcode.json explicitly (no conversion is performed)")
        raise FileNotFoundError(f"kernel configuration is required: {path}")
    if path.stat().st_size > 256 * 1024:
        raise ValueError("kernel configuration exceeds 256 KiB")
    return parse_kernel_config(json.loads(path.read_text(encoding="utf-8")))


def open_kernel(
    workspace: Path,
    *,
    state_dir: Path | None = None,
    trusted_plugins: frozenset[str] = frozenset(),
) -> Kernel:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    config = load_config(workspace)
    state_dir = state_directory(workspace) if state_dir is None else state_dir.resolve()
    if state_dir.is_relative_to(workspace):
        raise ValueError("kernel state must be outside the workspace writable by file tools")
    bundled = Path(__file__).resolve().parents[1] / "plugins"
    sources = {"files": bundled / "files", "openai-chat": bundled / "openai_chat"}
    entries = {name: config.plugins.get(name, KernelPluginEntry()) for name in sources}
    entries.update(config.plugins)
    if not trusted_plugins <= entries.keys():
        raise ValueError("trust was supplied for an unconfigured plugin")
    providers: dict[str, object] = {}
    public_providers: dict[str, object] = {}
    for name, entry in config.providers.items():
        if entry.base_url is None:
            raise ValueError(f"provider {name} requires an explicit base_url")
        key = entry.api_key or os.environ.get(f"{name.upper().replace('-', '_')}_API_KEY")
        providers[name] = {"api_key": key, "base_url": entry.base_url, "wire": entry.wire}
        public_providers[name] = {"base_url": entry.base_url, "wire": entry.wire}

    admitted: dict[str, HostPlugin] = {}
    identities: dict[str, object] = {}
    for plugin_id, entry in sorted(entries.items()):
        if not entry.enabled:
            continue
        external = entry.path is not None
        if external:
            source = workspace / str(entry.path)
        elif plugin_id in sources:
            source = sources[plugin_id]
        else:
            raise ValueError(f"plugin {plugin_id} requires an explicit path")
        plugin_config = dict(entry.config)
        public_config = dict(entry.config)
        if plugin_id == "openai-chat":
            if "providers" in plugin_config:
                raise ValueError("configure provider credentials/endpoints in the root providers section")
            plugin_config["providers"] = providers
            public_config["providers"] = public_providers
        plugin = admit_plugin(
            source,
            artifacts=state_dir / "artifacts",
            trusted=not external or plugin_id in trusted_plugins,
            config=plugin_config,
            capabilities=frozenset({"workspace.read", "workspace.write", "network"}),
        )
        if plugin.manifest.plugin_id != plugin_id:
            raise ValueError("configured plugin identity differs from its manifest")
        admitted[plugin_id] = plugin
        identities[plugin_id] = {
            "manifest": plugin.manifest.as_payload(),
            "artifact": plugin.root.name,
            # Never persist credentials or arbitrary plugin configuration in events/revisions.
            "config_sha256": hashlib.sha256(canonical_json(public_config).encode("utf-8")).hexdigest(),
        }
    graph = {name: set(plugin.manifest.depends) for name, plugin in admitted.items()}
    if any(dependency not in admitted for dependencies in graph.values() for dependency in dependencies):
        raise ValueError("plugin dependency is missing or disabled")
    order = tuple(TopologicalSorter(graph).static_order())
    sdk_root = Path(__file__).resolve().parents[1] / "sdk"
    sdk_hash = hashlib.sha256()
    for path in sorted(sdk_root.rglob("*.py")):
        sdk_hash.update(path.relative_to(sdk_root).as_posix().encode("utf-8") + b"\0")
        sdk_hash.update(path.read_bytes())
    payload = {
        "sdk_protocol": 1,
        "sdk_sha256": sdk_hash.hexdigest(),
        "python": sys.implementation.cache_tag,
        "dependencies": {name: version(name) for name in ("openai", "pydantic", "jsonschema")},
        "model": {"model": config.model.model, "reasoning_effort": config.model.reasoning_effort},
        "policy": {"approval_mode": config.policy.approval_mode, "read_only": config.policy.read_only},
        "plugins": identities,
    }
    return Kernel(workspace=workspace, state_dir=state_dir, config=config, plugins=tuple(admitted[name] for name in order), revision_payload=payload)
