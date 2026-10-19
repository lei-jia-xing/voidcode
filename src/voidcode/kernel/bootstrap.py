"""Product wiring: source trust, new configuration, and ordinary plugin roots."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path

from ..sdk.config import CONFIG_FILE_NAME, KernelConfig, KernelPluginEntry, parse_kernel_config
from ..sdk.manifest import MANIFEST_FILE_NAME, parse_plugin_manifest
from .activation import (
    DEFAULT_CAPABILITIES,
    Admission,
    AdmittedPlugins,
    admit_live,
    canonical_source_path,
    config_from_snapshot,
    load_private_snapshot,
    restore_admission,
    sdk_source_hash,
    write_private_snapshot,
)
from .engine import Kernel
from .storage.store import canonical_json, inspect_existing_workspace


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
    state_dir = state_directory(workspace) if state_dir is None else state_dir.resolve()
    if state_dir.is_relative_to(workspace):
        raise ValueError("kernel state must be outside the workspace writable by file tools")
    database = state_dir / "kernel-v1.sqlite3"
    sdk_root = Path(__file__).resolve().parents[1] / "sdk"

    # Explicit startup trust is captured ONCE, as canonical source paths. Reload never maps an
    # id to a live path again, so changing a plugin's configured path cannot inherit it.
    startup_paths: dict[str, str] = {}
    if trusted_plugins:
        live = load_config(workspace)
        entries = _merged_entries(live)
        startup_paths = {name: _configured_source(workspace, entries, name) for name in trusted_plugins}

    # Read-only inspection first: a fresh state directory must not write anything (and an
    # existing one must not be created or migrated) before configuration is validated.
    current_revision, current_payload = inspect_existing_workspace(database, str(workspace))

    def admit_current(authorized: frozenset[str]) -> Admission:
        """Re-admit the live configuration, trusting only pre-authorized canonical source paths."""
        live = load_config(workspace)
        private_providers, _public = _providers(live)
        admitted = admit_live(
            live,
            workspace=workspace,
            state_dir=state_dir,
            bundled_sources=_bundled_sources(),
            authorized_paths=authorized | frozenset(startup_paths.values()),
            capabilities=DEFAULT_CAPABILITIES,
            plugin_config=lambda plugin_id, source, config: _plugin_config(plugin_id, config, source=source, private_providers=private_providers),
        )
        return _admission_from(admitted, config=live, entries=_merged_entries(live))

    if current_revision is None:
        # First adoption: the live configuration becomes the active revision. Its private
        # snapshot is written before the pointer, so a crash leaves either the old state or
        # a recoverable snapshot, and the workspace is verified free before any write here.
        config = load_config(workspace)
        _assert_workspace_free(workspace)
        admission = admit_current(frozenset())
        write_private_snapshot(state_dir, admission.revision_id, admission.private_snapshot)
    else:
        # A restarted kernel restores the persisted revision and its private configuration
        # snapshot; changed workspace source or configuration is never admitted implicitly.
        if current_payload is None:
            raise ValueError(f"workspace revision {current_revision} disappeared from the kernel database")
        if current_payload.get("sdk_sha256") != sdk_source_hash(sdk_root):
            raise ValueError("installed SDK source no longer matches the active revision; this needs a controlled kernel upgrade, not a hot reload")
        snapshot = load_private_snapshot(state_dir, current_revision)
        config = config_from_snapshot(snapshot)
        admission = restore_admission(current_payload, snapshot, state_dir=state_dir, config=config)
    return Kernel(
        workspace=workspace,
        state_dir=state_dir,
        config=config,
        plugins=admission.plugins,
        revision_payload=admission.revision_payload,
        admission=admission,
        admits=admit_current,
    )


def _assert_workspace_free(workspace: Path) -> None:
    """Verify no other kernel owns the workspace before this process writes anything for it."""
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another kernel owns this workspace") from exc
    finally:
        os.close(descriptor)


def _bundled_sources() -> dict[str, Path]:
    """The plugin sources shipped with the kernel: id -> source directory.

    A bundled id is always trusted (the source ships with the kernel, so trusting it says
    nothing about user code); anything else needs an explicit `path` and explicit trust.
    """
    bundled = Path(__file__).resolve().parents[1] / "plugins"
    #: The shipped default-on plugin set: every product mechanism is an ordinary plugin, so the
    #: bundled list is the product surface. A workspace can disable any of them by id.
    ids = (
        "agents",
        "ast_grep",
        "btw",
        "compaction",
        "cron",
        "delegation",
        "effectiveness",
        "exec",
        "files",
        "formatter",
        "goal",
        "hooks",
        "lsp",
        "mcp",
        "plan",
        "providers",
        "review",
        "session_info",
        "skills",
        "todo",
        "user_shell",
        "web",
    )
    return {plugin_id: bundled / plugin_id.replace("-", "_") for plugin_id in ids}


def _merged_entries(config: KernelConfig) -> dict[str, KernelPluginEntry]:
    entries = {name: config.plugins.get(name, KernelPluginEntry()) for name in _bundled_sources()}
    entries.update(config.plugins)
    return entries


def _providers(config: KernelConfig) -> tuple[dict[str, object], dict[str, object]]:
    """Resolve private provider credentials and the redacted view that may reach revisions.

    Provider names are opaque: the kernel neither knows nor validates a wire protocol. Each name
    reaches the plugin that declared it as a provider, flattened to the vendor's own vocabulary
    plus the two typed fields the kernel resolves itself.
    """
    private: dict[str, object] = {}
    public: dict[str, object] = {}
    for name, entry in config.providers.items():
        if entry.base_url is None:
            raise ValueError(f"provider {name} requires an explicit base_url")
        key = entry.api_key or os.environ.get(f"{name.upper().replace('-', '_')}_API_KEY")
        private[name] = {**entry.extra, "api_key": key, "base_url": entry.base_url}
        public[name] = {"base_url": entry.base_url}
    return private, public


def _declared_providers(source: Path) -> frozenset[str]:
    """Provider names a plugin source declares, read without importing or admitting it."""
    manifest = parse_plugin_manifest(json.loads((source / MANIFEST_FILE_NAME).read_text(encoding="utf-8")))
    return frozenset(item.name for item in manifest.providers)


def _plugin_config(
    plugin_id: str,
    config: Mapping[str, object],
    *,
    source: Path,
    private_providers: Mapping[str, object],
) -> Mapping[str, object]:
    """Inject endpoint credentials for exactly the provider names this plugin declares."""
    resolved = dict(config)
    declared = _declared_providers(source)
    configured = declared.intersection(private_providers)
    if configured:
        if "providers" in resolved:
            raise ValueError(f"plugin {plugin_id} must configure provider credentials in the root providers section")
        resolved["providers"] = {name: private_providers[name] for name in sorted(configured)}
    return resolved


def _configured_source(workspace: Path, entries: Mapping[str, KernelPluginEntry], plugin_id: str) -> str:
    """Canonical path an explicitly trusted startup flag authorizes for one configured plugin."""
    if plugin_id not in entries:
        raise ValueError("trust was supplied for an unconfigured plugin")
    entry = entries[plugin_id]
    if entry.path is None:
        return canonical_source_path(_bundled_sources()[plugin_id])
    return canonical_source_path(workspace / str(entry.path))


def _validate_providers(config: KernelConfig, admitted: AdmittedPlugins) -> None:
    """A configured provider name must be a name some admitted plugin actually declares."""
    if not config.providers:
        return
    declared = {item.name for plugin in admitted.plugins for item in plugin.manifest.providers}
    missing = sorted(set(config.providers) - declared)
    if missing:
        raise ValueError(f"provider {missing[0]!r} is not declared by any admitted plugin")
    model = config.model.model
    if model is not None:
        provider_name, separator, model_name = model.partition("/")
        if not separator or not model_name or provider_name not in config.providers:
            raise ValueError("model must name a configured provider/model")
        if provider_name not in declared:
            raise ValueError(f"model provider {provider_name!r} is not declared by any admitted plugin")


def _admission_from(admitted: AdmittedPlugins, *, config: KernelConfig, entries: Mapping[str, KernelPluginEntry]) -> Admission:
    """Assemble the public revision payload and the private snapshot for one admitted generation."""
    _validate_providers(config, admitted)
    private_providers, public_providers = _providers(config)
    identities: dict[str, object] = {}
    for plugin in admitted.plugins:
        plugin_id = plugin.manifest.plugin_id
        entry = entries[plugin_id]
        public_config = dict(entry.config)
        declared_names = sorted({item.name for item in plugin.manifest.providers}.intersection(public_providers))
        if declared_names:
            public_config["providers"] = {name: public_providers[name] for name in declared_names}
        identities[plugin_id] = {
            "manifest": plugin.manifest.as_payload(),
            "artifact": plugin.root.name,
            # Never persist credentials or arbitrary plugin configuration in events/revisions.
            "config_sha256": hashlib.sha256(canonical_json(public_config).encode("utf-8")).hexdigest(),
        }
    snapshot_plugins: dict[str, object] = {}
    for source in admitted.sources:
        entry = entries[source.plugin_id]
        snapshot_plugins[source.plugin_id] = {
            "enabled": entry.enabled,
            "external": source.external,
            "path": None if entry.path is None else str(entry.path),
            "canonical_path": source.canonical_path,
            "trusted": source.trusted,
            "capabilities": sorted(admitted.plugin_capabilities.get(source.plugin_id, frozenset())),
            "entry_config": dict(entry.config),
            "config": dict(admitted.plugin_configs.get(source.plugin_id, entry.config)),
        }
    snapshot: dict[str, object] = {
        "version": 1,
        "providers": private_providers,
        "model": {"model": config.model.model, "reasoning_effort": config.model.reasoning_effort},
        "policy": {"approval_mode": config.policy.approval_mode, "read_only": config.policy.read_only},
        "plugins": snapshot_plugins,
        "authorized_paths": sorted(admitted.authorized_paths),
    }
    payload: dict[str, object] = {
        "sdk_protocol": 1,
        "sdk_sha256": sdk_source_hash(Path(__file__).resolve().parents[1] / "sdk"),
        "python": sys.implementation.cache_tag,
        "dependencies": {name: version(name) for name in ("openai", "pydantic", "jsonschema")},
        "model": {"model": config.model.model, "reasoning_effort": config.model.reasoning_effort},
        "policy": {"approval_mode": config.policy.approval_mode, "read_only": config.policy.read_only},
        "plugins": identities,
        # Digest only: the credential-bearing configuration stays in the private snapshot.
        "private_config_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    }
    return Admission(
        plugins=admitted.plugins,
        revision_payload=payload,
        private_snapshot=snapshot,
        authorized_paths=admitted.authorized_paths,
        sources=admitted.sources,
        config=config,
    )
