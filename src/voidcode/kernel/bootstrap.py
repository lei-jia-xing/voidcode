"""Product wiring: source trust, new configuration, and ordinary plugin roots."""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from .. import __version__
from ..sdk.config import CONFIG_FILE_NAME, KernelConfig, KernelPluginEntry, parse_kernel_config
from ..sdk.json_values import json_wire_object
from ..sdk.manifest import MANIFEST_FILE_NAME, PluginManifest, parse_plugin_manifest
from ..sdk.plugin import HostPlugin
from .activation import (
    DEFAULT_CAPABILITIES,
    Admission,
    AdmittedPlugins,
    PluginSource,
    admit_live,
    admit_plugin,
    canonical_source_path,
    config_from_snapshot,
    load_private_snapshot,
    plan_state_copies,
    restore_admission,
    sdk_source_hash,
    write_private_snapshot,
)
from .engine import Kernel
from .host import HostClient
from .images import IMAGES_DIR_NAME, Image, installed_identity, running_identity
from .images import pin as image_pin
from .storage.store import SCHEMA_VERSION, EventDraft, SqliteStore, canonical_json, inspect_existing_workspace


@functools.cache
def _executing_image() -> Image:
    """The image this process executes: its pinned launch identity, or the installed tree.

    The first writer runs inside a materialized image instead (CLI routing), so what gets pinned
    is always recoverable; the plain-tree digest covers unsupervised entry points that never
    adopt by themselves.
    """
    pinned = running_identity()
    return pinned if pinned is not None else installed_identity()


def _launch_protocol() -> int:
    """The supervisor<->child launch interface this build speaks (the preflight refusal vocabulary)."""
    from .preflight import LAUNCH_PROTOCOL_VERSION

    return LAUNCH_PROTOCOL_VERSION


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
    auto_drain: bool = True,
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
    current_revision, current_payload, recorded_schema = inspect_existing_workspace(database, str(workspace))

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
        if current_payload is None or recorded_schema is None:
            raise ValueError(f"workspace revision {current_revision} disappeared from the kernel database")
        if recorded_schema != SCHEMA_VERSION:
            raise ValueError(
                f"workspace state is at schema {recorded_schema}; this build reads schema {SCHEMA_VERSION}: "
                "run the explicit upgrade transition through the supervisor "
                "(`voidcode serve --session-id <id>` for the first adoption of this build, "
                "or `voidcode upgrade --candidate <install>`) instead of starting the kernel directly"
            )
        _check_image_pin(state_dir, current_payload)
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
        auto_drain=auto_drain,
    )


def _check_image_pin(state_dir: Path, payload: Mapping[str, object]) -> None:
    """Refuse to serve state another image was elected for, with the recovery spelled out."""
    recorded = payload.get("image")
    if not isinstance(recorded, Mapping):
        return  # pre-image revisions name no election
    digest = recorded.get("digest")
    if not isinstance(digest, str) or not digest:
        return
    if digest != _executing_image().digest:
        raise ValueError(
            f"this state was elected to image {digest}; start that image "
            f"({state_dir / IMAGES_DIR_NAME / digest}) instead of {_executing_image().digest}"
        )


def _recorded_host_plugins(
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifests: Mapping[str, PluginManifest],
    *,
    state_dir: Path,
) -> tuple[HostPlugin, ...]:
    """The host inputs of the currently recorded generation: verified pins, no source reads."""
    identities = payload.get("plugins")
    snapshot_plugins = snapshot.get("plugins")
    if not isinstance(identities, Mapping) or not isinstance(snapshot_plugins, Mapping):
        raise ValueError("recorded revision is missing its plugin identities")
    plugins: list[HostPlugin] = []
    for plugin_id in sorted(identities):
        entry = snapshot_plugins.get(plugin_id)
        identity = identities[plugin_id]
        if not isinstance(entry, Mapping) or entry.get("enabled") is not True or not isinstance(identity, Mapping):
            continue
        artifact = identity.get("artifact")
        config = entry.get("config")
        caps = entry.get("capabilities")
        if not isinstance(artifact, str) or not artifact or not isinstance(config, Mapping):
            raise ValueError(f"recorded identity for {plugin_id} is malformed")
        if not isinstance(caps, list) or not all(isinstance(item, str) for item in caps):
            raise ValueError(f"recorded capabilities for {plugin_id} must be a list of strings")
        plugins.append(
            HostPlugin(
                root=state_dir / "artifacts" / artifact,
                manifest=manifests[plugin_id],
                config=json_wire_object(config),
                capabilities=frozenset(caps),
            )
        )
    return tuple(plugins)


def _adopt_target_plugins(
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifests: Mapping[str, PluginManifest],
    *,
    state_dir: Path,
) -> tuple[tuple[HostPlugin, ...], dict[str, PluginManifest]]:
    """The host inputs this build would install: bundled sources from this image plus recorded pins.

    A bundled plugin the candidate image no longer ships is simply absent from the target (the
    existing deactivation transform owns that case); an external plugin reuses its recorded
    artifact byte-for-byte and its workspace source is never re-read.
    """
    identities = payload.get("plugins")
    snapshot_plugins = snapshot.get("plugins")
    if not isinstance(identities, Mapping) or not isinstance(snapshot_plugins, Mapping):
        raise ValueError("recorded revision is missing its plugin identities")
    plugins: list[HostPlugin] = []
    target: dict[str, PluginManifest] = {}
    for plugin_id in sorted(identities):
        entry = snapshot_plugins.get(plugin_id)
        identity = identities[plugin_id]
        if not isinstance(entry, Mapping) or entry.get("enabled") is not True or not isinstance(identity, Mapping):
            continue
        config = entry.get("config")
        caps = entry.get("capabilities")
        if not isinstance(config, Mapping):
            raise ValueError(f"recorded configuration for {plugin_id} is missing or not an object")
        if not isinstance(caps, list) or not all(isinstance(item, str) for item in caps):
            raise ValueError(f"recorded capabilities for {plugin_id} must be a list of strings")
        if entry.get("external") is True:
            artifact = identity.get("artifact")
            if not isinstance(artifact, str) or not artifact:
                raise ValueError(f"recorded identity for {plugin_id} is malformed")
            plugins.append(
                HostPlugin(
                    root=state_dir / "artifacts" / artifact,
                    manifest=manifests[plugin_id],
                    config=json_wire_object(config),
                    capabilities=frozenset(caps),
                )
            )
            target[plugin_id] = manifests[plugin_id]
            continue
        source = Path(__file__).resolve().parents[1] / "plugins" / plugin_id.replace("-", "_")
        if not source.is_dir():
            continue
        plugin = admit_plugin(
            source,
            artifacts=state_dir / "artifacts",
            trusted=True,
            config=dict(config),
            capabilities=frozenset(caps),
        )
        if plugin.manifest.plugin_id != plugin_id:
            raise ValueError(f"candidate bundled source for {plugin_id} carries another identity")
        plugins.append(plugin)
        target[plugin_id] = plugin.manifest
    return tuple(plugins), target


def _snapshot_entries(snapshot: Mapping[str, object]) -> tuple[dict[str, KernelPluginEntry], tuple[PluginSource, ...], frozenset[str]]:
    """The recorded configuration, sources, and path authorizations, rebuilt from one snapshot."""
    snapshot_plugins = snapshot.get("plugins")
    authorized = snapshot.get("authorized_paths")
    if not isinstance(snapshot_plugins, Mapping):
        raise ValueError("private configuration snapshot has no plugin entries")
    if not isinstance(authorized, list) or not all(isinstance(item, str) for item in authorized):
        raise ValueError("private configuration snapshot authorized_paths must be a list of strings")
    entries: dict[str, KernelPluginEntry] = {}
    sources: list[PluginSource] = []
    for plugin_id in sorted(snapshot_plugins):
        entry = snapshot_plugins[plugin_id]
        if not isinstance(plugin_id, str) or not isinstance(entry, Mapping):
            raise ValueError("private plugin entries must map plugin ids to entry objects")
        canonical = entry.get("canonical_path")
        if not isinstance(canonical, str) or not canonical:
            raise ValueError(f"private plugin entry {plugin_id} has no canonical source path")
        for flag in ("enabled", "external", "trusted"):
            if type(entry.get(flag)) is not bool:
                raise ValueError(f"private plugin entry {plugin_id} field {flag} must be a boolean")
        entry_path = entry.get("path")
        entry_config = entry.get("entry_config")
        if entry_path is not None and not isinstance(entry_path, str):
            raise ValueError(f"private plugin entry {plugin_id} field path must be a string or null")
        if not isinstance(entry_config, Mapping):
            raise ValueError(f"private plugin entry {plugin_id} has no recorded entry configuration")
        entries[plugin_id] = KernelPluginEntry(enabled=entry["enabled"], config=dict(entry_config), path=entry_path)
        sources.append(
            PluginSource(
                plugin_id=plugin_id,
                canonical_path=canonical,
                external=entry["external"],
                enabled=entry["enabled"],
                trusted=entry["trusted"],
            )
        )
    return entries, tuple(sources), frozenset(authorized)


def adopt_generation(*, workspace: Path, state_dir: Path, session_id: str | None) -> dict[str, object]:
    """Adopt this build as the workspace's new generation and return the transition report.

    Bundled plugins are pinned from this installation, external plugins reuse their recorded
    artifacts, plugin state copies are proven by the two throwaway hosts, and then one transaction
    moves the schema, the pointer, every session ref, and every state copy. An adoption needs an
    acting session whenever the workspace has a session; it never reads the live configuration.
    """
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be a directory")
    database = state_dir / "kernel-v1.sqlite3"
    store = SqliteStore(database, allow_schema=frozenset({4, 5}))
    try:
        expected_revision = store.workspace_current(str(workspace))
        if expected_revision is None:
            raise ValueError("adoption needs an adopted workspace; run a normal command for first adoption instead")
        recorded = store.revision(expected_revision)
        snapshot = load_private_snapshot(state_dir, expected_revision)
        config = config_from_snapshot(snapshot)
        identities = recorded.get("plugins")
        if not isinstance(identities, Mapping):
            raise ValueError("recorded revision has no plugin identities")
        manifests = {
            plugin_id: parse_plugin_manifest(json_wire_object(identity["manifest"]))
            for plugin_id, identity in identities.items()
            if isinstance(identity, Mapping)
        }
        if set(manifests) != set(identities):
            raise ValueError("recorded revision has a malformed plugin identity")
        target_plugins, target_manifests = _adopt_target_plugins(recorded, snapshot, manifests, state_dir=state_dir)
        recorded_plugins = _recorded_host_plugins(recorded, snapshot, manifests, state_dir=state_dir)

        candidate_host: HostClient | None = None
        current_host: HostClient | None = None
        try:
            candidate_host = HostClient(target_plugins, workspace=workspace, revision_id="adopt-target", store=None)
            current_host = HostClient(recorded_plugins, workspace=workspace, revision_id=expected_revision, store=None)
            session_ids = [record.session_id for record in store.sessions(workspace=str(workspace))]
            plan = plan_state_copies(
                store=store,
                workspace=str(workspace),
                session_ids=session_ids,
                old_manifests=manifests,
                candidate_manifests=target_manifests,
                migrate=lambda owner, payload, from_version, to_version: candidate_host.migrate_owner_state(
                    owner, payload, from_version=from_version, to_version=to_version
                ),
                deactivate=lambda owner, payload, scope, scope_key, tip: current_host.prepare_deactivate_owner_state(
                    owner, payload, scope=scope, scope_key=scope_key, tip_sequence=tip
                ),
            )
        finally:
            if candidate_host is not None:
                candidate_host.close()
            if current_host is not None:
                current_host.close()

        entries, sources, authorized_paths = _snapshot_entries(snapshot)
        snapshot_plugins = snapshot.get("plugins")
        if not isinstance(snapshot_plugins, Mapping):
            raise ValueError("private configuration snapshot has no plugin entries")
        plugin_configs: dict[str, Mapping[str, object]] = {}
        plugin_capabilities: dict[str, frozenset[str]] = {}
        for plugin_id, entry in snapshot_plugins.items():
            if not isinstance(plugin_id, str) or not isinstance(entry, Mapping):
                raise ValueError("private plugin entries must map plugin ids to entry objects")
            entry_config = entry.get("config")
            caps = entry.get("capabilities")
            if not isinstance(entry_config, Mapping):
                raise ValueError(f"private plugin entry {plugin_id} has no recorded configuration")
            if not isinstance(caps, list) or not all(isinstance(item, str) for item in caps):
                raise ValueError(f"private plugin entry {plugin_id} capabilities must be a list of strings")
            plugin_configs[plugin_id] = json_wire_object(entry_config)
            plugin_capabilities[plugin_id] = frozenset(caps)
        admitted = AdmittedPlugins(
            plugins=target_plugins,
            sources=sources,
            authorized_paths=authorized_paths,
            plugin_configs=plugin_configs,
            plugin_capabilities=plugin_capabilities,
        )
        admission = _admission_from(admitted, config=config, entries=entries)
        write_private_snapshot(state_dir, admission.revision_id, admission.private_snapshot)

        session = store.session(session_id) if session_id is not None else None
        checkpoint = store.checkpoint(session_id) if session_id is not None else None
        checkpoint_payload = None
        checkpoint_kind: Literal["ready", "approval_wait", "terminal", "interrupted"] | None = None
        if checkpoint is not None and isinstance(checkpoint.payload, Mapping):
            checkpoint_payload = json_wire_object(checkpoint.payload)
            # The checkpoints CHECK constraint admits exactly these four kinds.
            checkpoint_kind = cast("Literal['ready', 'approval_wait', 'terminal', 'interrupted']", checkpoint.kind)
        run_identity = uuid4().hex if session is None else (session.run_id or uuid4().hex)
        events = (
            (
                EventDraft(
                    "kernel.revision_adopted",
                    "kernel",
                    "kernel",
                    {
                        "request_id": None,
                        "source": "adoption",
                        "from_revision_id": expected_revision,
                        "to_revision_id": admission.revision_id,
                        "image_digest": _executing_image().digest,
                        "sdk_sha256": str(admission.revision_payload.get("sdk_sha256", "")),
                        "migrated_owners": list(plan.migrated_owners),
                        "deactivated_owners": list(plan.deactivated_owners),
                    },
                ),
            )
            if session is not None
            else ()
        )
        store.adopt_revision(
            workspace=str(workspace),
            session_id=session_id,
            expected_sequence=None if session is None else session.last_event_sequence,
            expected_run_id=None if session is None else session.run_id,
            expected_revision_id=expected_revision,
            target_revision_id=admission.revision_id,
            target_payload=admission.revision_payload,
            run_id=run_identity,
            status=None if session is None else session.status,
            events=events,
            checkpoint_kind=checkpoint_kind,
            checkpoint_payload=checkpoint_payload,
            state_updates=plan.updates,
            cancelled_owners=plan.deactivated_owners,
            cleared_budget_owners=plan.deactivated_owners,
            require_quiescent=True,
        )
        return {
            "from_revision_id": expected_revision,
            "to_revision_id": admission.revision_id,
            "migrated_owners": list(plan.migrated_owners),
            "deactivated_owners": list(plan.deactivated_owners),
        }
    finally:
        store.close()


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
        "image": image_pin(_executing_image(), kernel_version=__version__, launch_protocol=_launch_protocol()),
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
