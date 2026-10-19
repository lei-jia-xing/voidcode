"""Admission pins trusted source bytes; it never imports the candidate plugin."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from graphlib import TopologicalSorter
from pathlib import Path

import jsonschema

from ..sdk.config import (
    KernelConfig,
    KernelModelConfig,
    KernelPluginEntry,
    KernelPolicyConfig,
    KernelProviderEntry,
)
from ..sdk.json_values import json_wire_object, own_json_object
from ..sdk.manifest import MANIFEST_FILE_NAME, PluginFileIdentity, PluginManifest, parse_plugin_manifest
from ..sdk.plugin import HostPlugin
from .storage.store import (
    SqliteStore,
    StagedStateUpdate,
    canonical_json,
    revision_identity,
)

__all__ = [
    "Admission",
    "AdmittedPlugins",
    "config_from_snapshot",
    "PluginSource",
    "StateCopyPlan",
    "admit_live",
    "admit_plugin",
    "load_private_snapshot",
    "plan_state_copies",
    "restore_admission",
    "sdk_source_hash",
    "snapshot_path",
    "validate_state_payload",
    "write_private_snapshot",
]

#: Invocation capabilities granted to every admitted plugin by the product wiring.
DEFAULT_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "network"})

_PRIVATE_DIR_NAME = "private"


def admit_plugin(
    source: Path,
    *,
    artifacts: Path,
    trusted: bool,
    config: Mapping[str, object],
    capabilities: frozenset[str],
) -> HostPlugin:
    if not trusted:
        raise PermissionError("plugin source requires explicit user trust before admission")
    source = source.resolve(strict=True)
    manifest = parse_plugin_manifest(json.loads((source / MANIFEST_FILE_NAME).read_text(encoding="utf-8")))
    if not manifest.files:
        raise ValueError(f"plugin {manifest.plugin_id} must declare implementation files")
    declared = {item.path for item in manifest.files}
    entry_module = manifest.entrypoint.partition(":")[0].replace(".", "/")
    if f"{entry_module}.py" not in declared and f"{entry_module}/__init__.py" not in declared:
        raise ValueError("plugin entrypoint is not in its declared implementation files")
    for path in source.rglob("*.py"):
        if path.relative_to(source).as_posix() not in declared:
            raise ValueError(f"plugin has undeclared Python source: {path.relative_to(source)}")
    requested = frozenset(item.name for item in manifest.capabilities)
    if not requested <= capabilities:
        raise PermissionError(f"plugin {manifest.plugin_id} requests ungranted capabilities")
    try:
        jsonschema.Draft202012Validator(json_wire_object(manifest.config_schema)).validate(json_wire_object(config))
    except jsonschema.ValidationError as exc:
        raise ValueError(f"plugin {manifest.plugin_id} configuration failed schema validation") from exc

    manifest_json = canonical_json(manifest.as_payload())
    identity = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    artifacts.mkdir(parents=True, exist_ok=True)
    destination = artifacts / identity
    if destination.exists():
        _verify_artifact(destination, manifest_json, manifest.files)
    else:
        with tempfile.TemporaryDirectory(prefix=".admit-", dir=artifacts) as temporary:
            candidate = Path(temporary) / "root"
            candidate.mkdir()
            for item in manifest.files:
                origin = (source / item.path).resolve(strict=True)
                if not origin.is_relative_to(source) or not origin.is_file():
                    raise ValueError("plugin file escapes its source root or is not a regular file")
                target = candidate / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                with origin.open("rb") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                with target.open("rb") as reader:
                    digest = hashlib.file_digest(reader, "sha256").hexdigest()
                if digest != item.sha256:
                    raise ValueError(f"plugin file identity changed: {item.path}")
            (candidate / MANIFEST_FILE_NAME).write_text(manifest_json, encoding="utf-8")
            try:
                os.rename(candidate, destination)
            except FileExistsError:
                _verify_artifact(destination, manifest_json, manifest.files)
    return HostPlugin(destination, manifest, config, requested)


def _verify_artifact(root: Path, manifest_json: str, identities: tuple[PluginFileIdentity, ...]) -> None:
    if (root / MANIFEST_FILE_NAME).read_text(encoding="utf-8") != manifest_json:
        raise ValueError("admitted plugin manifest was modified")
    for item in identities:
        path = (root / item.path).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("admitted plugin resource escaped its artifact root")
        with path.open("rb") as reader:
            digest = hashlib.file_digest(reader, "sha256").hexdigest()
        if digest != item.sha256:
            raise ValueError(f"admitted plugin artifact was modified: {item.path}")


@dataclass(frozen=True, slots=True)
class PluginSource:
    """Where one configured plugin id currently comes from, and whether that path is authorized."""

    plugin_id: str
    canonical_path: str
    external: bool
    enabled: bool
    trusted: bool


@dataclass(frozen=True, slots=True)
class AdmittedPlugins:
    """Pinned plugin artifacts plus the authorization set one live admission established."""

    plugins: tuple[HostPlugin, ...]
    sources: tuple[PluginSource, ...]
    authorized_paths: frozenset[str]
    plugin_configs: Mapping[str, Mapping[str, object]]
    plugin_capabilities: Mapping[str, frozenset[str]]

    @property
    def plugin_ids(self) -> frozenset[str]:
        return frozenset(plugin.manifest.plugin_id for plugin in self.plugins)


@dataclass(frozen=True, slots=True)
class Admission:
    """One immutable capability generation: pinned artifacts, public payload, private config."""

    plugins: tuple[HostPlugin, ...]
    revision_payload: Mapping[str, object]
    private_snapshot: Mapping[str, object]
    authorized_paths: frozenset[str]
    sources: tuple[PluginSource, ...]
    config: KernelConfig

    def __post_init__(self) -> None:
        # Own read-only snapshots: callers cannot mutate the generation they were handed.
        object.__setattr__(self, "revision_payload", own_json_object(self.revision_payload))
        object.__setattr__(self, "private_snapshot", own_json_object(self.private_snapshot))

    @property
    def revision_id(self) -> str:
        return revision_identity(self.revision_payload)

    @property
    def plugin_ids(self) -> frozenset[str]:
        return frozenset(plugin.manifest.plugin_id for plugin in self.plugins)

    @property
    def manifests(self) -> Mapping[str, PluginManifest]:
        return {plugin.manifest.plugin_id: plugin.manifest for plugin in self.plugins}


def sdk_source_hash(sdk_root: Path) -> str:
    """Pin the SDK source bytes a host process will import, so a mutation never hides behind a revision."""
    digest = hashlib.sha256()
    for path in sorted(sdk_root.rglob("*.py")):
        digest.update(path.relative_to(sdk_root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def canonical_source_path(path: Path) -> str:
    """Canonicalize a configured source path for authorization, existing or not."""
    return os.path.realpath(os.fspath(path))


def admit_live(
    config: KernelConfig,
    *,
    workspace: Path,
    state_dir: Path,
    bundled_sources: Mapping[str, Path],
    trusted_ids: frozenset[str] = frozenset(),
    authorized_paths: frozenset[str] = frozenset(),
    capabilities: frozenset[str] = DEFAULT_CAPABILITIES,
    plugin_config: Callable[[str, Path, Mapping[str, object]], Mapping[str, object]] | None = None,
) -> AdmittedPlugins:
    """Admit every configured plugin from its configured source; never imports candidate code.

    Owns source resolution, the trust decision bound to canonical source paths, artifact
    pinning, admission and dependency closure. The product wiring owns configuration
    reading, credential injection (`plugin_config`) and the payload shapes.
    """
    entries = {name: config.plugins.get(name, KernelPluginEntry()) for name in bundled_sources}
    entries.update(config.plugins)
    unknown_trust = trusted_ids - entries.keys()
    if unknown_trust:
        raise ValueError("trust was supplied for an unconfigured plugin")
    newly_authorized: set[str] = set()
    sources: list[PluginSource] = []
    admitted: dict[str, HostPlugin] = {}
    final_configs: dict[str, Mapping[str, object]] = {}
    for plugin_id, entry in sorted(entries.items()):
        external = entry.path is not None
        if external:
            source = workspace / str(entry.path)
        elif plugin_id in bundled_sources:
            source = bundled_sources[plugin_id]
        else:
            raise ValueError(f"plugin {plugin_id} requires an explicit path")
        canonical = canonical_source_path(source)
        trusted = not external or canonical in authorized_paths or plugin_id in trusted_ids
        if external and plugin_id in trusted_ids:
            newly_authorized.add(canonical)
        sources.append(PluginSource(plugin_id=plugin_id, canonical_path=canonical, external=external, enabled=entry.enabled, trusted=trusted))
        if not entry.enabled:
            continue
        resolved_config = dict(plugin_config(plugin_id, source, dict(entry.config))) if plugin_config is not None else dict(entry.config)
        plugin = admit_plugin(
            source,
            artifacts=state_dir / "artifacts",
            trusted=trusted,
            config=resolved_config,
            capabilities=capabilities,
        )
        if plugin.manifest.plugin_id != plugin_id:
            raise ValueError("configured plugin identity differs from its manifest")
        admitted[plugin_id] = plugin
        final_configs[plugin_id] = resolved_config
    graph = {name: set(plugin.manifest.depends) for name, plugin in admitted.items()}
    if any(dependency not in admitted for dependencies in graph.values() for dependency in dependencies):
        raise ValueError("plugin dependency is missing or disabled")
    order = tuple(TopologicalSorter(graph).static_order())
    return AdmittedPlugins(
        plugins=tuple(admitted[name] for name in order),
        sources=tuple(sources),
        authorized_paths=frozenset(authorized_paths | newly_authorized),
        plugin_configs={name: final_configs[name] for name in admitted},
        plugin_capabilities={name: capabilities for name in admitted},
    )


def restore_admission(
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    *,
    state_dir: Path,
    config: KernelConfig,
) -> Admission:
    """Rebuild a generation from durable records without reading workspace source or config.

    Every field is required and type-checked: a stored snapshot either restores exactly or
    the kernel refuses to open, and it never invents provenance defaults for missing data.
    """
    payload = json_wire_object(payload)
    snapshot = json_wire_object(snapshot)
    digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
    if payload.get("private_config_sha256") != digest:
        raise ValueError("private configuration snapshot does not match the revision it belongs to")
    plugins_spec = payload.get("plugins")
    if not isinstance(plugins_spec, Mapping):
        raise ValueError("revision payload has no plugin identities")
    snapshot_plugins = snapshot.get("plugins")
    if not isinstance(snapshot_plugins, Mapping):
        raise ValueError("private configuration snapshot has no plugin entries")
    manifests: dict[str, PluginManifest] = {}
    roots: dict[str, Path] = {}
    for plugin_id, identity in plugins_spec.items():
        if not isinstance(plugin_id, str) or not isinstance(identity, Mapping):
            raise ValueError("revision plugin identities must map plugin ids to identity objects")
        manifest_payload = identity.get("manifest")
        artifact = identity.get("artifact")
        if not isinstance(manifest_payload, Mapping) or not isinstance(artifact, str) or not artifact:
            raise ValueError(f"revision identity for {plugin_id} is malformed")
        manifest = parse_plugin_manifest(manifest_payload)
        if manifest.plugin_id != plugin_id:
            raise ValueError(f"revision identity {plugin_id} disagrees with its manifest plugin id {manifest.plugin_id}")
        root = (state_dir / "artifacts" / artifact).resolve(strict=True)
        _verify_artifact(root, canonical_json(manifest.as_payload()), manifest.files)
        manifests[plugin_id] = manifest
        roots[plugin_id] = root
    graph = {name: set(manifest.depends) for name, manifest in manifests.items()}
    if any(dependency not in manifests for dependencies in graph.values() for dependency in dependencies):
        raise ValueError("revision payload has an incomplete dependency closure")
    order = tuple(TopologicalSorter(graph).static_order())
    plugins: list[HostPlugin] = []
    for plugin_id in order:
        entry = snapshot_plugins.get(plugin_id)
        if not isinstance(entry, Mapping):
            raise ValueError(f"private configuration snapshot is missing plugin {plugin_id}")
        plugin_config = entry.get("config")
        if not isinstance(plugin_config, Mapping):
            raise ValueError(f"private plugin configuration for {plugin_id} is missing or not an object")
        caps = entry.get("capabilities")
        if not isinstance(caps, list) or not all(isinstance(item, str) for item in caps):
            raise ValueError(f"private plugin capabilities for {plugin_id} must be a list of strings")
        plugins.append(
            HostPlugin(
                root=roots[plugin_id],
                manifest=manifests[plugin_id],
                config=plugin_config,
                capabilities=frozenset(caps),
            )
        )
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
        path = entry.get("path")
        if path is not None and not isinstance(path, str):
            raise ValueError(f"private plugin entry {plugin_id} field path must be a string or null")
        sources.append(
            PluginSource(
                plugin_id=plugin_id,
                canonical_path=canonical,
                external=entry["external"],
                enabled=entry["enabled"],
                trusted=entry["trusted"],
            )
        )
    authorized = snapshot.get("authorized_paths")
    if not isinstance(authorized, list) or not all(isinstance(item, str) for item in authorized):
        raise ValueError("private configuration snapshot authorized_paths must be a list of strings")
    return Admission(
        plugins=tuple(plugins),
        revision_payload=payload,
        private_snapshot=snapshot,
        authorized_paths=frozenset(authorized),
        sources=tuple(sources),
        config=config,
    )


def snapshot_path(state_dir: Path, revision_id: str) -> Path:
    return state_dir / _PRIVATE_DIR_NAME / f"{revision_id}.json"


def write_private_snapshot(state_dir: Path, revision_id: str, snapshot: Mapping[str, object]) -> Path:
    """Persist credential-bearing configuration once per revision identity, never overwriting one."""
    directory = state_dir / _PRIVATE_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = snapshot_path(state_dir, revision_id)
    text = canonical_json(snapshot)
    if target.exists():
        if target.read_text(encoding="utf-8") != text:
            raise ValueError(f"private configuration snapshot for revision {revision_id} already exists with other content")
        return target
    temporary = directory / f".{revision_id}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            if target.read_text(encoding="utf-8") != text:
                raise ValueError(f"private configuration snapshot for revision {revision_id} already exists with other content") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_private_snapshot(state_dir: Path, revision_id: str) -> Mapping[str, object]:
    """Load the private configuration a stored revision was admitted with, or fail visibly."""
    path = snapshot_path(state_dir, revision_id)
    if not path.is_file():
        raise FileNotFoundError(
            f"private configuration snapshot is missing for the active revision {revision_id} ({path}); "
            "restore it from backup or start a fresh kernel state directory"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"private configuration snapshot {path} is not an object")
    return json_wire_object(payload)


def validate_state_payload(payload: Mapping[str, object], manifest: PluginManifest | None) -> Mapping[str, object]:
    """Require an owner state copy to be a JSON object, and match its declared schema when it has one."""
    owned = json_wire_object(payload)
    if manifest is not None and manifest.state_schema is not None:
        try:
            jsonschema.Draft202012Validator(json_wire_object(manifest.state_schema)).validate(owned)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"state copy failed {manifest.plugin_id} state_schema: {exc.message}") from exc
    return owned


@dataclass(frozen=True, slots=True)
class StateCopyPlan:
    """The owner state copies one activation transaction will write, and who they belong to."""

    updates: tuple[StagedStateUpdate, ...]
    migrated_owners: tuple[str, ...]
    deactivated_owners: tuple[str, ...]


def plan_state_copies(
    *,
    store: SqliteStore,
    workspace: str,
    session_ids: Sequence[str],
    old_manifests: Mapping[str, PluginManifest],
    candidate_manifests: Mapping[str, PluginManifest],
    migrate: Callable[[str, Mapping[str, object], int, int], Mapping[str, object]],
    deactivate: Callable[[str, Mapping[str, object], str, str, int | None], Mapping[str, object]],
) -> StateCopyPlan:
    """Copy final owner state into the candidate generation with the right pure transform per row.

    Every row is transformed at most once: an owner admitted by the candidate is migrated to
    the candidate's declared state schema version (or validated against it when no migration is
    needed), an owner the candidate does not admit is deactivated by the host that still holds
    its schema, and an owner that is neither stays byte-identical.
    """
    updates: list[StagedStateUpdate] = []
    migrated: list[str] = []
    deactivated: list[str] = []
    deactivate_owners = frozenset(old_manifests) - frozenset(candidate_manifests)
    scopes: list[tuple[str, str]] = [("session", session_id) for session_id in session_ids]
    scopes.append(("workspace", workspace))
    for scope, scope_key in scopes:
        tip = store.session(scope_key).last_event_sequence if scope == "session" else None
        for row in store.plugin_state_rows(scope, scope_key):
            manifest = candidate_manifests.get(row.owner)
            if manifest is not None:
                target_version = manifest.state_schema_version
                if target_version < row.schema_version:
                    raise ValueError(
                        f"plugin {row.owner} state is at schema version {row.schema_version}, "
                        f"but the candidate only declares {target_version}: downgrades are not supported"
                    )
                if target_version == row.schema_version:
                    # Even an unchanged schema version must be able to hold this state.
                    validate_state_payload(row.payload, manifest)
                    continue
                payload = validate_state_payload(migrate(row.owner, row.payload, row.schema_version, target_version), manifest)
                updates.append(
                    StagedStateUpdate(
                        owner=row.owner,
                        scope=scope,
                        scope_key=scope_key,
                        payload=payload,
                        expected_version=row.version,
                        schema_version=target_version,
                    )
                )
                migrated.append(row.owner)
            elif row.owner in deactivate_owners:
                payload = validate_state_payload(deactivate(row.owner, row.payload, scope, scope_key, tip), old_manifests[row.owner])
                updates.append(
                    StagedStateUpdate(
                        owner=row.owner,
                        scope=scope,
                        scope_key=scope_key,
                        payload=payload,
                        expected_version=row.version,
                        schema_version=row.schema_version,
                    )
                )
                deactivated.append(row.owner)
    return StateCopyPlan(updates=tuple(updates), migrated_owners=tuple(sorted(set(migrated))), deactivated_owners=tuple(sorted(set(deactivated))))


def config_from_snapshot(snapshot: Mapping[str, object]) -> KernelConfig:
    """Rebuild the effective configuration a stored revision was admitted with."""
    providers = snapshot.get("providers")
    model = snapshot.get("model")
    policy = snapshot.get("policy")
    plugins = snapshot.get("plugins")
    if not isinstance(providers, Mapping) or not isinstance(model, Mapping) or not isinstance(policy, Mapping) or not isinstance(plugins, Mapping):
        raise ValueError("private configuration snapshot is missing providers, model, policy or plugins")
    return KernelConfig(
        model=KernelModelConfig(model=model.get("model"), reasoning_effort=model.get("reasoning_effort")),
        policy=KernelPolicyConfig(approval_mode=policy.get("approval_mode", "ask"), read_only=policy.get("read_only", False)),
        providers={
            name: KernelProviderEntry(
                api_key=entry.get("api_key"),
                base_url=entry.get("base_url"),
                # The private snapshot holds the flattened vendor vocabulary, so the typed
                # fields are reconstructed only to be flattened back on the next admission.
                extra={key: value for key, value in entry.items() if key not in {"api_key", "base_url"}},
            )
            for name, entry in providers.items()
            if isinstance(entry, Mapping)
        },
        plugins={
            plugin_id: KernelPluginEntry(
                enabled=entry["enabled"],
                config=entry["entry_config"],
                path=entry.get("path"),
            )
            for plugin_id, entry in plugins.items()
            if isinstance(entry, Mapping)
        },
    )
