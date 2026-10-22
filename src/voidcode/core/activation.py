"""Plugin admission: configured trusted sources become the runtime inputs of one generation.

Plugins are trusted code equal to the application. Admission resolves each configured plugin's
source directory against the installation or the workspace, injects the resolved provider
credentials the core owns, and hands the runtime the same runtime objects a plugin factory will
register. Nothing is copied, hashed, or pinned: a restart re-imports the current source with the
recorded configuration, and a generation is a composition snapshot with an opaque id. The private
snapshot (0600) is the only place credentials are persisted.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import jsonschema

from ..sdk.config import CoreConfig, CorePluginEntry, parse_core_config
from ..sdk.json_values import json_wire_object, own_json_object
from ..sdk.plugin import StateSchema
from .plugins import PluginSource
from .storage.store import (
    PluginStateRow,
    SessionRecord,
    StagedStateUpdate,
    canonical_json,
)

__all__ = [
    "Admission",
    "BUNDLED_PLUGIN_IDS",
    "PRIVATE_SNAPSHOT_VERSION",
    "admit_live",
    "bundled_sources",
    "config_from_snapshot",
    "load_private_snapshot",
    "merged_entries",
    "plan_state_copies",
    "resolved_providers",
    "restore_admission",
    "snapshot_path",
    "validate_providers",
    "validate_state_payload",
    "write_private_snapshot",
]

#: The private snapshot shape this build reads and writes. Older shapes are refused, never guessed.
PRIVATE_SNAPSHOT_VERSION = 2

_PRIVATE_DIR_NAME = "private"

#: The shipped default-on plugin set: every product mechanism is an ordinary plugin, so this
#: list is the product surface. A workspace can disable any of them by id.
BUNDLED_PLUGIN_IDS = (
    "ast_grep",
    "btw",
    "compaction",
    "cron",
    "delegation",
    "effectiveness",
    "exec",
    "files",
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


def bundled_sources(package_root: Path | None = None) -> dict[str, Path]:
    """The plugin sources shipped with one installation: id -> source directory."""
    root = Path(__file__).resolve().parents[1] if package_root is None else Path(package_root)
    bundled = root / "plugins"
    return {plugin_id: bundled / plugin_id.replace("-", "_") for plugin_id in BUNDLED_PLUGIN_IDS}


@dataclass(frozen=True, slots=True)
class Admission:
    """One immutable capability generation: opaque id, runtime inputs, payload, private configuration."""

    revision_id: str
    plugins: tuple[PluginSource, ...]
    revision_payload: Mapping[str, object]
    private_snapshot: Mapping[str, object]
    config: CoreConfig

    def __post_init__(self) -> None:
        if not isinstance(self.revision_id, str) or not self.revision_id:
            raise ValueError("admission needs a non-empty generation id")
        # Own read-only snapshots: callers cannot mutate the generation they were handed.
        object.__setattr__(self, "revision_payload", own_json_object(self.revision_payload))
        object.__setattr__(self, "private_snapshot", own_json_object(self.private_snapshot))

    @property
    def plugin_ids(self) -> frozenset[str]:
        return frozenset(plugin.plugin_id for plugin in self.plugins)


def merged_entries(config: CoreConfig, bundled_sources_: Mapping[str, Path]) -> dict[str, CorePluginEntry]:
    """Every configured entry: the shipped bundled defaults first, the user's entries on top.

    Bundled ids are the product surface; the configuration can disable or reconfigure one by id,
    and can add an external plugin with an explicit ``path``.
    """
    entries = {plugin_id: config.plugins.get(plugin_id, CorePluginEntry()) for plugin_id in bundled_sources_}
    entries.update(config.plugins)
    return entries


def resolved_providers(config: CoreConfig) -> dict[str, object]:
    """Resolve the private provider vocabulary once: configured fields plus the environment key.

    Provider names are opaque: the core neither knows nor validates a wire protocol. Each name
    reaches every plugin factory as the reserved ``providers`` config key, flattened to the
    vendor's own vocabulary plus the two typed fields the core resolves itself.
    """
    private: dict[str, object] = {}
    for name, entry in config.providers.items():
        if entry.base_url is None:
            raise ValueError(f"provider {name} requires an explicit base_url")
        key = entry.api_key or os.environ.get(f"{name.upper().replace('-', '_')}_API_KEY")
        private[name] = {**entry.extra, "api_key": key, "base_url": entry.base_url}
    return private


def _plugin_root(
    workspace: Path,
    bundled: Mapping[str, Path],
    plugin_id: str,
    entry: CorePluginEntry,
) -> Path:
    if entry.path is not None:
        root = (workspace / entry.path).resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"plugin {plugin_id} path is not a directory: {root}")
    else:
        source = bundled.get(plugin_id)
        if source is None:
            raise ValueError(f"plugin {plugin_id} requires an explicit path")
        if not source.is_dir():
            raise ValueError(f"bundled plugin {plugin_id} is missing from this installation: {source}")
        root = source.resolve(strict=True)
    if not (root / "__init__.py").is_file():
        raise ValueError(f"plugin {plugin_id} source is not a package with an __init__.py: {root}")
    return root


def admit_live(
    config: CoreConfig,
    *,
    workspace: Path,
    bundled_sources: Mapping[str, Path],
) -> tuple[PluginSource, ...]:
    """Resolve every enabled configured plugin to a runtime input; imports no plugin code.

    The resolved provider credentials are injected as the reserved ``providers`` config key on
    every plugin factory config; a plugin may not configure provider credentials itself.
    """
    workspace = Path(workspace).resolve(strict=True)
    providers = resolved_providers(config)
    plugins: list[PluginSource] = []
    for plugin_id, entry in sorted(merged_entries(config, bundled_sources).items()):
        if not entry.enabled:
            continue
        root = _plugin_root(workspace, bundled_sources, plugin_id, entry)
        if "providers" in entry.config:
            raise ValueError(f"plugin {plugin_id} must configure provider credentials in the root providers section")
        resolved = dict(entry.config)
        if providers:
            resolved["providers"] = providers
        plugins.append(PluginSource(root=root, plugin_id=plugin_id, config=resolved))
    return tuple(plugins)


def restore_admission(
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    *,
    revision_id: str,
    workspace: Path,
    bundled_sources: Mapping[str, Path],
) -> Admission:
    """Rebuild a generation from durable records, importing the CURRENT plugin source.

    The recorded configuration and credentials are the truth; the source bytes are the ones this
    installation ships now (no byte pin exists). Everything is type-checked: a stored snapshot
    either restores exactly or the core refuses to open.
    """
    payload = json_wire_object(payload)
    snapshot = json_wire_object(snapshot)
    digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
    if payload.get("private_config_sha256") != digest:
        raise ValueError("private configuration snapshot does not match the revision it belongs to")
    config = config_from_snapshot(snapshot)
    plugins = admit_live(config, workspace=workspace, bundled_sources=bundled_sources)
    recorded = payload.get("plugins")
    if not isinstance(recorded, list) or not all(isinstance(plugin_id, str) for plugin_id in recorded):
        raise ValueError("revision payload has no plugin composition")
    if set(recorded) != {plugin.plugin_id for plugin in plugins}:
        raise ValueError("recorded plugin composition does not match the recorded configuration")
    return Admission(
        revision_id=revision_id,
        plugins=plugins,
        revision_payload=payload,
        private_snapshot=snapshot,
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
            "restore it from backup or start a fresh core state directory"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"private configuration snapshot {path} is not an object")
    return json_wire_object(payload)


def validate_state_payload(payload: Mapping[str, object], state: StateSchema | None) -> Mapping[str, object]:
    """Require an owner state copy to be a JSON object, and match its declared schema when it has one."""
    owned = json_wire_object(payload)
    if state is not None and state.schema is not None:
        try:
            jsonschema.Draft202012Validator(json_wire_object(state.schema)).validate(owned)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"state copy failed its declared state schema: {exc.message}") from exc
    return owned


def validate_providers(config: CoreConfig, registered: Sequence[str]) -> None:
    """A configured provider name must be a name some admitted plugin actually registered.

    The init catalog is the proof of registration; the configured root section is the authority
    for which names may resolve credentials. A typo fails visibly instead of silently disabling
    model execution.
    """
    if not config.providers:
        return
    names = set(registered)
    missing = sorted(set(config.providers) - names)
    if missing:
        raise ValueError(f"provider {missing[0]!r} is not registered by any admitted plugin")
    model = config.model.model
    if model is not None:
        provider_name, separator, model_name = model.partition("/")
        if not separator or not model_name or provider_name not in config.providers:
            raise ValueError("model must name a configured provider/model")
        if provider_name not in names:
            raise ValueError(f"model provider {provider_name!r} is not registered by any admitted plugin")


@dataclass(frozen=True, slots=True)
class StateCopyPlan:
    """The owner state copies one activation transaction will write, and who they belong to."""

    updates: tuple[StagedStateUpdate, ...]
    migrated_owners: tuple[str, ...]
    deactivated_owners: tuple[str, ...]


class StateCopySource(Protocol):
    """The two store reads the state-copy planner needs."""

    def plugin_state_rows(self, scope: str, scope_key: str) -> tuple[PluginStateRow, ...]: ...
    def session(self, session_id: str) -> SessionRecord: ...


def plan_state_copies(
    *,
    store: StateCopySource,
    workspace: str,
    session_ids: Sequence[str],
    old_states: Mapping[str, StateSchema],
    candidate_states: Mapping[str, StateSchema],
    migrate: Callable[[str, Mapping[str, object], int, int], Mapping[str, object]],
    deactivate: Callable[[str, Mapping[str, object], str, str, int | None], Mapping[str, object]],
) -> StateCopyPlan:
    """Copy final owner state into the candidate generation with the right pure transform per row.

    Every row is transformed at most once: an owner admitted by the candidate is migrated to
    the candidate's declared state schema version (or validated against it when no migration is
    needed), an owner the candidate does not admit is deactivated by the runtime that still holds
    its schema, and an owner that is neither stays byte-identical.
    """
    updates: list[StagedStateUpdate] = []
    migrated: list[str] = []
    deactivated: list[str] = []
    deactivate_owners = frozenset(old_states) - frozenset(candidate_states)
    scopes: list[tuple[str, str]] = [("session", session_id) for session_id in session_ids]
    scopes.append(("workspace", workspace))
    for scope, scope_key in scopes:
        tip = store.session(scope_key).last_event_sequence if scope == "session" else None
        for row in store.plugin_state_rows(scope, scope_key):
            candidate = candidate_states.get(row.owner)
            if candidate is not None:
                target_version = candidate.version
                if target_version < row.schema_version:
                    raise ValueError(
                        f"plugin {row.owner} state is at schema version {row.schema_version}, "
                        f"but the candidate only declares {target_version}: downgrades are not supported"
                    )
                if target_version == row.schema_version:
                    # Even an unchanged schema version must be able to hold this state.
                    validate_state_payload(row.payload, candidate)
                    continue
                payload = validate_state_payload(migrate(row.owner, row.payload, row.schema_version, target_version), candidate)
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
                payload = validate_state_payload(deactivate(row.owner, row.payload, scope, scope_key, tip), old_states[row.owner])
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


def config_from_snapshot(snapshot: Mapping[str, object]) -> CoreConfig:
    """Rebuild the effective configuration a stored revision was admitted with.

    The snapshot's model/policy/providers/plugins shape is the same shape ``voidcode.json`` has
    (the private snapshot is just credential-bearing), so the one strict parser validates it.
    """
    if snapshot.get("version") != PRIVATE_SNAPSHOT_VERSION:
        raise ValueError(
            "private configuration snapshot predates this build; this build reads only its own snapshot shape: start a fresh core state directory"
        )
    missing = [name for name in ("providers", "model", "policy", "plugins") if name not in snapshot]
    if missing:
        raise ValueError("private configuration snapshot is missing " + ", ".join(missing))
    sections: dict[str, object] = {}
    for name in ("model", "policy", "providers", "plugins"):
        section = snapshot[name]
        if not isinstance(section, Mapping):
            raise ValueError(f"private configuration snapshot section {name!r} must be an object")
        sections[name] = json_wire_object(section)
    return parse_core_config({"schema_version": 1, **sections})
