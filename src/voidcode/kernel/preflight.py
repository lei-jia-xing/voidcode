"""Candidate preflight: everything a kernel upgrade would change, proven before downtime.

The first half of this module never executes candidate code and never writes: it opens the
database strictly read-only, verifies the recorded revision, its pinned artifacts and private
snapshot, checks plugin state versions, and diffs the recorded generation against the candidate
installation. The second half - the dry-run - executes the candidate's plugin code inside
throwaway host processes pinned into a scratch tree under the candidate image: admission, state
migration transforms, deactivation transforms, and a real host load of the target generation.
The dry-run is trusted exactly as the upgrade ingress is (the user named this candidate on the
command line), and it cannot touch live state: its artifacts live in a removed-afterwards scratch
tree, its hosts are constructed with no store and no callbacks, and the database is only ever
opened through the same read-only reader as the first half.

Nothing here may emit a successful report for an incompatible generation: an unsupported protocol
refuses with its own reason instead of merely flagging a change, and the dry-run's failures are
refusals, not warnings.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..sdk.json_values import json_wire_object
from ..sdk.manifest import MANIFEST_FILE_NAME, PluginManifest, parse_plugin_manifest
from ..sdk.plugin import HostPlugin
from .activation import _verify_artifact, admit_plugin, load_private_snapshot, plan_state_copies, validate_state_payload
from .host import HostClient, HostTimeout, HostUnavailable
from .images import IMAGES_DIR_NAME
from .storage.store import (
    SCHEMA_MAGIC,
    SCHEMA_VERSION,
    ForeignDatabaseError,
    PluginStateRow,
    SessionRecord,
    canonical_json,
    open_read_only,
)

__all__ = [
    "LAUNCH_PROTOCOL_VERSION",
    "SUPPORTED_TRANSITIONS",
    "BundledChange",
    "DryRunReport",
    "PreflightRefused",
    "PreflightReport",
    "SchemaPlan",
    "assemble_target_generation",
    "bundled_artifact_identity",
    "read_connection",
    "read_plugin_state",
    "read_workspace_revision",
    "schema_plan",
    "verify_dry_run",
    "verify_generation_diff",
    "verify_plugin_state",
    "verify_preflight",
    "verify_recorded_generation",
]

#: Explicitly supported schema transitions: ``(recorded, target) -> name``. An equal pair needs no
#: transition at all; everything else is refused before any downtime.
SUPPORTED_TRANSITIONS: Mapping[tuple[int, int], str] = {(4, 5): "schema_4_to_5"}

#: The supervisor<->child launch interface this build speaks. A candidate that does not speak it is
#: refused before downtime; a supported conversion is never implied.
LAUNCH_PROTOCOL_VERSION = 1

#: Bundled plugin sources live under ``plugins/<id with underscores>`` in an installation.
_PLUGINS_DIR_NAME = "plugins"


class PreflightRefused(Exception):
    """A preflight finding that stops the upgrade before anything is stopped or written."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SchemaPlan:
    """The schema situation of one database against the candidate's expected version."""

    recorded: int
    target: int
    transition: str | None

    @property
    def unchanged(self) -> bool:
        return self.transition is None


@dataclass(frozen=True, slots=True)
class BundledChange:
    """One bundled plugin whose candidate installation differs from the recorded generation."""

    plugin_id: str
    recorded_artifact: str
    candidate_artifact: str | None

    def as_payload(self) -> dict[str, object]:
        return {"plugin_id": self.plugin_id, "from_artifact": self.recorded_artifact, "to_artifact": self.candidate_artifact}


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """What the throwaway target generation proved: its load and its state-copy plan."""

    target_plugins: tuple[str, ...]
    migrated_owners: tuple[str, ...]
    deactivated_owners: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Everything a preflight established about one candidate, read-only and dry-run together."""

    schema: SchemaPlan
    recorded_revision_id: str
    recorded_sdk_sha256: str
    candidate_sdk_sha256: str
    recorded_sdk_protocol: int
    candidate_sdk_protocol: int
    candidate_launch_protocol: int
    image_changed: bool
    bundled_changes: tuple[BundledChange, ...]
    migrated_owners: tuple[str, ...]
    deactivated_owners: tuple[str, ...]
    plugin_state: tuple[Mapping[str, object], ...]
    image_digest: str
    web_bundle: str | None

    @property
    def adoption_required(self) -> bool:
        """Whether the candidate would build a new generation rather than restore the recorded one."""
        return not self.schema.unchanged or self.image_changed or self.recorded_sdk_sha256 != self.candidate_sdk_sha256 or bool(self.bundled_changes)

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "reason": None,
            "detail": "",
            "db_schema": self.schema.recorded,
            "target_schema": self.schema.target,
            "transition": self.schema.transition,
            "recorded_revision_id": self.recorded_revision_id,
            "recorded_sdk_sha256": self.recorded_sdk_sha256,
            "candidate_sdk_sha256": self.candidate_sdk_sha256,
            "sdk_protocol": self.recorded_sdk_protocol,
            "launch_protocol": self.candidate_launch_protocol,
            "adoption_required": self.adoption_required,
            "image_changed": self.image_changed,
            "bundled_changes": [change.as_payload() for change in self.bundled_changes],
            "migrated_owners": list(self.migrated_owners),
            "deactivated_owners": list(self.deactivated_owners),
            "plugin_state": [dict(row) for row in self.plugin_state],
            "image_digest": self.image_digest,
            "web_bundle": self.web_bundle,
        }


def read_connection(database: Path) -> sqlite3.Connection:
    """Open one kernel database through the store's read-only reader, mapped to refusal reasons."""
    if not Path(database).exists():
        raise PreflightRefused("db_missing", f"kernel database does not exist: {database}")
    try:
        return open_read_only(database)
    except ForeignDatabaseError as exc:
        raise PreflightRefused("db_magic", str(exc)) from exc


def schema_plan(connection: sqlite3.Connection, *, target: int = SCHEMA_VERSION) -> SchemaPlan:
    """The recorded schema version against the candidate's, with the transition it needs."""
    row = connection.execute("SELECT magic, schema_version FROM kernel_identity WHERE id = 1").fetchone()
    if row is None:
        raise PreflightRefused("db_magic", "database has no kernel identity row")
    if _as_str(row["magic"]) != SCHEMA_MAGIC:
        raise PreflightRefused("db_magic", f"unsupported database magic {row['magic']!r}")
    recorded = _as_int(row["schema_version"])
    if recorded == target:
        return SchemaPlan(recorded=recorded, target=target, transition=None)
    transition = SUPPORTED_TRANSITIONS.get((recorded, target))
    if transition is not None:
        return SchemaPlan(recorded=recorded, target=target, transition=transition)
    if recorded > target:
        raise PreflightRefused("db_schema_newer", f"recorded schema {recorded} is newer than the candidate's {target}")
    raise PreflightRefused("db_schema_unsupported_transition", f"no supported transition from schema {recorded} to {target}")


def read_workspace_revision(connection: sqlite3.Connection, workspace: str) -> tuple[str, Mapping[str, object]]:
    """The revision one workspace currently runs, with its payload."""
    row = connection.execute("SELECT active_revision_id FROM workspace_state WHERE workspace = ?", (workspace,)).fetchone()
    if row is None:
        raise PreflightRefused("no_adopted_revision", f"workspace {workspace} has no adopted revision")
    revision_id = _as_str(row["active_revision_id"])
    payload_row = connection.execute("SELECT payload_json FROM revisions WHERE revision_id = ?", (revision_id,)).fetchone()
    if payload_row is None:
        raise PreflightRefused("revision_payload_invalid", f"revision {revision_id} is not stored")
    try:
        payload = json.loads(_as_str(payload_row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise PreflightRefused("revision_payload_invalid", f"revision {revision_id} payload is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PreflightRefused("revision_payload_invalid", f"revision {revision_id} payload is not an object")
    return revision_id, json_wire_object(payload)


def verify_recorded_generation(
    state_dir: Path,
    revision_id: str,
    payload: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, PluginManifest]]:
    """Verify the private snapshot and every pinned artifact of one recorded revision.

    Returns the snapshot and the manifests by plugin id. Any mismatch is a refusal: the candidate
    would otherwise start a generation whose bytes nobody verified.
    """
    snapshot = load_private_snapshot(state_dir, revision_id)
    digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
    if payload.get("private_config_sha256") != digest:
        raise PreflightRefused("snapshot_digest_mismatch", f"private snapshot for {revision_id} does not match its revision digest")
    plugins = payload.get("plugins")
    if not isinstance(plugins, Mapping):
        raise PreflightRefused("revision_payload_invalid", f"revision {revision_id} has no plugin identities")
    # An empty generation is legal: a workspace may disable every plugin. There is simply
    # nothing to verify or load beyond the snapshot digest, which was already checked.
    manifests: dict[str, PluginManifest] = {}
    for plugin_id, identity in plugins.items():
        if not isinstance(plugin_id, str) or not isinstance(identity, Mapping):
            raise PreflightRefused("revision_payload_invalid", "plugin identities must map ids to identity objects")
        manifest_payload = identity.get("manifest")
        artifact = identity.get("artifact")
        if not isinstance(manifest_payload, Mapping) or not isinstance(artifact, str) or not artifact:
            raise PreflightRefused("revision_payload_invalid", f"identity for {plugin_id} is malformed")
        manifest = parse_plugin_manifest(manifest_payload)
        if manifest.plugin_id != plugin_id:
            raise PreflightRefused("revision_payload_invalid", f"identity {plugin_id} disagrees with its manifest id {manifest.plugin_id}")
        root = state_dir / "artifacts" / artifact
        if not root.is_dir():
            raise PreflightRefused("artifact_missing", f"pinned artifact {artifact} for {plugin_id} is missing")
        try:
            _verify_artifact(root.resolve(strict=True), canonical_json(manifest.as_payload()), manifest.files)
        except (OSError, ValueError) as exc:
            raise PreflightRefused("artifact_modified", f"pinned artifact for {plugin_id} failed verification: {exc}") from exc
        manifests[plugin_id] = manifest
    return snapshot, manifests


def read_plugin_state(connection: sqlite3.Connection) -> tuple[Mapping[str, object], ...]:
    """Every owner state row, in a stable order, without writing anything."""
    rows = connection.execute(
        """
        SELECT owner, scope, scope_key, payload_json, version, schema_version, revision_id
        FROM plugin_state
        ORDER BY scope ASC, scope_key ASC, owner ASC
        """
    ).fetchall()
    state: list[Mapping[str, object]] = []
    for row in rows:
        try:
            payload = json.loads(_as_str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise PreflightRefused("plugin_state_invalid", f"state row {row['owner']!r} is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise PreflightRefused("plugin_state_invalid", f"state row {row['owner']!r} is not an object")
        state.append(
            {
                "owner": _as_str(row["owner"]),
                "scope": _as_str(row["scope"]),
                "scope_key": _as_str(row["scope_key"]),
                "payload": json_wire_object(payload),
                "version": _as_int(row["version"]),
                "schema_version": _as_int(row["schema_version"]),
                "revision_id": _as_str(row["revision_id"]),
            }
        )
    return tuple(state)


def verify_plugin_state(
    rows: Sequence[Mapping[str, object]],
    manifests: Mapping[str, PluginManifest],
) -> tuple[Mapping[str, object], ...]:
    """Check every owner state row against the manifest its generation pinned.

    A row above its manifest's declared version is a downgrade and refuses; a row at the declared
    version must satisfy that manifest's ``state_schema``; a row below it is reported and left alone
    (forward migration is proven separately by the dry-run, never by rewriting live rows here).
    """
    reported: list[Mapping[str, object]] = []
    for row in rows:
        owner = _as_str(row["owner"])
        declared_version = _as_int(row["schema_version"])
        manifest = manifests.get(owner)
        entry: dict[str, object] = {
            "owner": owner,
            "scope": _as_str(row["scope"]),
            "scope_key": _as_str(row["scope_key"]),
            "schema_version": declared_version,
        }
        if manifest is not None:
            target_version = manifest.state_schema_version
            if declared_version > target_version:
                raise PreflightRefused(
                    "plugin_state_downgrade",
                    f"plugin {owner} state is at schema version {declared_version} while the pinned manifest declares {target_version}",
                )
            if declared_version == target_version:
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    raise PreflightRefused("plugin_state_invalid", f"plugin {owner} state row is malformed")
                try:
                    validate_state_payload(payload, manifest)
                except ValueError as exc:
                    raise PreflightRefused("plugin_state_invalid", f"plugin {owner} state failed its declared schema: {exc}") from exc
                entry["validated"] = True
            else:
                entry["migration_owed"] = target_version
        reported.append(entry)
    return tuple(reported)


def bundled_artifact_identity(manifest: PluginManifest) -> str:
    """The content address ``admit_plugin`` gives one plugin source, without admitting it."""
    return hashlib.sha256(canonical_json(manifest.as_payload()).encode("utf-8")).hexdigest()


def verify_generation_diff(
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifests: Mapping[str, PluginManifest],
    *,
    candidate_package_root: Path,
    image_digest: str,
    candidate_sdk_protocol: int,
    candidate_launch_protocol: int,
) -> tuple[bool, tuple[BundledChange, ...]]:
    """Compare the recorded generation against the candidate installation, read-only.

    Unsupported protocols refuse outright: a generation built for another protocol is never
    "adopted" by this build. Returns ``(image_changed, bundled_changes)``; the SDK byte difference
    is reported by the caller, which already holds both hashes.
    """
    if payload.get("sdk_protocol") != candidate_sdk_protocol:
        raise PreflightRefused(
            "host_protocol_mismatch",
            f"recorded generation speaks SDK protocol {payload.get('sdk_protocol')!r}; the candidate speaks {candidate_sdk_protocol}",
        )
    recorded_image = payload.get("image")
    recorded_digest = recorded_image.get("digest") if isinstance(recorded_image, Mapping) else None
    image_changed = not isinstance(recorded_digest, str) or recorded_digest != image_digest
    recorded_launch = recorded_image.get("launch_protocol") if isinstance(recorded_image, Mapping) else None
    if recorded_launch is not None and recorded_launch != candidate_launch_protocol:
        raise PreflightRefused(
            "launch_protocol_mismatch",
            f"recorded generation speaks launch protocol {recorded_launch!r}; the candidate speaks {candidate_launch_protocol}",
        )
    snapshot_plugins = snapshot.get("plugins")
    if not isinstance(snapshot_plugins, Mapping):
        raise PreflightRefused("revision_payload_invalid", "private configuration snapshot has no plugin entries")
    changes: list[BundledChange] = []
    for plugin_id, recorded_manifest in sorted(manifests.items()):
        entry = snapshot_plugins.get(plugin_id)
        if not isinstance(entry, Mapping) or entry.get("external") is True or entry.get("enabled") is not True:
            continue
        recorded_artifact = bundled_artifact_identity(recorded_manifest)
        source_manifest = candidate_package_root / _PLUGINS_DIR_NAME / plugin_id.replace("-", "_") / MANIFEST_FILE_NAME
        if not source_manifest.is_file():
            changes.append(BundledChange(plugin_id=plugin_id, recorded_artifact=recorded_artifact, candidate_artifact=None))
            continue
        try:
            candidate_manifest = parse_plugin_manifest(json.loads(source_manifest.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise PreflightRefused("admission_failed", f"candidate manifest for bundled plugin {plugin_id} is unusable: {exc}") from exc
        candidate_artifact = bundled_artifact_identity(candidate_manifest)
        if candidate_artifact != recorded_artifact:
            changes.append(
                BundledChange(
                    plugin_id=plugin_id,
                    recorded_artifact=recorded_artifact,
                    candidate_artifact=candidate_artifact,
                )
            )
    return image_changed, tuple(changes)


def assemble_target_generation(
    *,
    state_dir: Path,
    artifacts_dir: Path,
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifests: Mapping[str, PluginManifest],
    candidate_package_root: Path,
) -> tuple[tuple[HostPlugin, ...], Mapping[str, PluginManifest]]:
    """Build the generation an adoption would install, pinning new bytes only into ``artifacts_dir``.

    The single assembly the dry-run and the adoption share: bundled ids are pinned from the
    candidate installation (they ship with the binary the user named), external ids reuse their
    recorded artifacts byte-for-byte and their workspace sources are never re-read, and a bundled
    id the candidate no longer ships is simply absent (the existing deactivation transform owns
    that case). Admission order follows the dependency closure, exactly like a live admission.
    """
    identities = payload.get("plugins")
    snapshot_plugins = snapshot.get("plugins")
    if not isinstance(identities, Mapping) or not isinstance(snapshot_plugins, Mapping):
        raise PreflightRefused("revision_payload_invalid", "recorded revision is missing its plugin identities")
    unordered: dict[str, HostPlugin] = {}
    target: dict[str, PluginManifest] = {}
    for plugin_id in sorted(identities):
        entry = snapshot_plugins.get(plugin_id)
        identity = identities[plugin_id]
        if not isinstance(entry, Mapping) or entry.get("enabled") is not True or not isinstance(identity, Mapping):
            continue
        config = entry.get("config")
        caps = entry.get("capabilities")
        if not isinstance(config, Mapping):
            raise PreflightRefused("revision_payload_invalid", f"recorded configuration for {plugin_id} is missing or not an object")
        if not isinstance(caps, list) or not all(isinstance(item, str) for item in caps):
            raise PreflightRefused("revision_payload_invalid", f"recorded capabilities for {plugin_id} must be a list of strings")
        if entry.get("external") is True:
            artifact = identity.get("artifact")
            if not isinstance(artifact, str) or not artifact:
                raise PreflightRefused("revision_payload_invalid", f"recorded identity for {plugin_id} is malformed")
            unordered[plugin_id] = HostPlugin(
                root=state_dir / "artifacts" / artifact,
                manifest=manifests[plugin_id],
                config=json_wire_object(config),
                capabilities=frozenset(caps),
            )
            target[plugin_id] = manifests[plugin_id]
            continue
        source = candidate_package_root / _PLUGINS_DIR_NAME / plugin_id.replace("-", "_")
        if not source.is_dir():
            continue
        try:
            plugin = admit_plugin(
                source,
                artifacts=artifacts_dir,
                trusted=True,
                config=dict(config),
                capabilities=frozenset(caps),
            )
        except (OSError, PermissionError, ValueError) as exc:
            raise PreflightRefused("admission_failed", f"candidate cannot admit bundled plugin {plugin_id}: {exc}") from exc
        if plugin.manifest.plugin_id != plugin_id:
            raise PreflightRefused("admission_failed", f"candidate bundled source for {plugin_id} carries another identity")
        unordered[plugin_id] = plugin
        target[plugin_id] = plugin.manifest
    graph = {name: set(target[name].depends) for name in unordered}
    if any(dependency not in unordered for dependencies in graph.values() for dependency in dependencies):
        raise PreflightRefused("admission_failed", "the candidate generation has an incomplete dependency closure")
    from graphlib import TopologicalSorter

    order = tuple(TopologicalSorter(graph).static_order())
    return tuple(unordered[name] for name in order), target


class _ReadOnlyStoreView:
    """The two store reads the state-copy planner needs, served from the rows just inspected."""

    def __init__(self, connection: sqlite3.Connection, rows: Sequence[Mapping[str, object]]) -> None:
        self._connection = connection
        self._rows = rows

    def close(self) -> None:
        self._connection.close()

    def plugin_state_rows(self, scope: str, scope_key: str) -> tuple[PluginStateRow, ...]:
        return tuple(
            PluginStateRow(
                owner=_as_str(row["owner"]),
                scope=_as_str(row["scope"]),
                scope_key=_as_str(row["scope_key"]),
                payload=json_wire_object(_require_mapping(row["payload"])),
                version=_as_int(row["version"]),
                schema_version=_as_int(row["schema_version"]),
                revision_id=_as_str(row["revision_id"]),
            )
            for row in self._rows
            if row["scope"] == scope and row["scope_key"] == scope_key
        )

    def session(self, session_id: str) -> SessionRecord:
        row = self._connection.execute(
            """
            SELECT session_id, workspace, status, active_revision_id, run_id, turn, last_event_sequence,
                   parent_session_id, root_session_id, depth, tool_allowlist_json
            FROM sessions
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"session {session_id!r} does not exist")
        return SessionRecord(
            session_id=_as_str(row["session_id"]),
            workspace=_as_str(row["workspace"]),
            status=_as_str(row["status"]),
            active_revision_id=_as_str(row["active_revision_id"]),
            run_id=None if row["run_id"] is None else _as_str(row["run_id"]),
            turn=_as_int(row["turn"]),
            last_event_sequence=_as_int(row["last_event_sequence"]),
            root_session_id=_as_str(row["root_session_id"]),
            parent_session_id=None if row["parent_session_id"] is None else _as_str(row["parent_session_id"]),
            depth=_as_int(row["depth"]),
            tool_allowlist=None,
        )


def verify_dry_run(
    *,
    state_dir: Path,
    workspace: Path,
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifests: Mapping[str, PluginManifest],
    rows: Sequence[Mapping[str, object]],
    session_ids: Sequence[str],
    candidate_package_root: Path,
    image_digest: str,
) -> DryRunReport:
    """Load the target generation in throwaway hosts and prove its state copies, discarding all.

    The candidate's plugin code executes here - that is the point of the check - but nothing can
    land in live state: new artifact bytes go to a scratch tree that is removed afterwards, both
    hosts are built with no store and no callbacks, and the state-copy plan is computed from the
    read-only rows and reported, never written.
    """
    scratch_parent = state_dir / IMAGES_DIR_NAME / image_digest / "scratch"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="dry-run-", dir=scratch_parent))
    try:
        target_plugins, target_manifests = assemble_target_generation(
            state_dir=state_dir,
            artifacts_dir=scratch / "artifacts",
            payload=payload,
            snapshot=snapshot,
            manifests=manifests,
            candidate_package_root=candidate_package_root,
        )
        recorded_plugins = tuple(
            plugin for plugin_id in sorted(manifests) for plugin in _recorded_plugin(payload, snapshot, manifests, state_dir, plugin_id)
        )
        try:
            current_host = HostClient(recorded_plugins, workspace=workspace, revision_id="dry-run-recorded", store=None)
        except Exception as exc:
            raise PreflightRefused("plugin_load_failed", f"the recorded generation does not load: {exc}") from exc
        try:
            try:
                candidate_host = HostClient(target_plugins, workspace=workspace, revision_id="dry-run-target", store=None)
            except Exception as exc:
                raise PreflightRefused("plugin_load_failed", f"the candidate generation does not load: {exc}") from exc
            try:
                view = _ReadOnlyStoreView(read_connection(state_dir / "kernel-v1.sqlite3"), rows)
                try:
                    plan = plan_state_copies(
                        store=view,
                        workspace=str(workspace),
                        session_ids=list(session_ids),
                        old_manifests=manifests,
                        candidate_manifests=target_manifests,
                        migrate=_migrate_with_owner(candidate_host),
                        deactivate=_deactivate_with_owner(current_host),
                    )
                except PreflightRefused:
                    raise
                except _TransformFailed as exc:
                    raise PreflightRefused("migration_failed", f"a state transform failed: {exc}") from exc
                except ValueError as exc:
                    raise PreflightRefused("migration_failed", f"a state copy failed: {exc}") from exc
                except (HostUnavailable, HostTimeout, TimeoutError, OSError, RuntimeError) as exc:
                    raise PreflightRefused("migration_failed", f"a state transform failed: {exc}") from exc
                finally:
                    view.close()
            finally:
                candidate_host.close()
        finally:
            current_host.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return DryRunReport(
        target_plugins=tuple(sorted(target_manifests)),
        migrated_owners=plan.migrated_owners,
        deactivated_owners=plan.deactivated_owners,
    )


class _TransformFailed(Exception):
    """A pure state transform failed, with the owner and direction it carried."""

    def __init__(self, owner: str, direction: str, detail: str) -> None:
        super().__init__(f"{owner} {direction}: {detail}")
        self.owner = owner
        self.direction = direction
        self.detail = detail


def _migrate_with_owner(candidate_host: HostClient) -> Callable[[str, Mapping[str, object], int, int], Mapping[str, object]]:
    def run(owner: str, payload: Mapping[str, object], from_version: int, to_version: int) -> Mapping[str, object]:
        try:
            return candidate_host.migrate_owner_state(owner, payload, from_version=from_version, to_version=to_version)
        except Exception as exc:
            raise _TransformFailed(owner, f"{from_version}->{to_version}", str(exc)) from exc

    return run


def _deactivate_with_owner(current_host: HostClient) -> Callable[[str, Mapping[str, object], str, str, int | None], Mapping[str, object]]:
    def run(owner: str, payload: Mapping[str, object], scope: str, scope_key: str, tip: int | None) -> Mapping[str, object]:
        direction = f"{scope}:{scope_key}" if tip is None else f"{scope}:{scope_key}@{tip}"
        try:
            return current_host.prepare_deactivate_owner_state(owner, payload, scope=scope, scope_key=scope_key, tip_sequence=tip)
        except Exception as exc:
            raise _TransformFailed(owner, direction, str(exc)) from exc

    return run


def _recorded_plugin(
    payload: Mapping[str, object],
    snapshot: Mapping[str, object],
    manifests: Mapping[str, PluginManifest],
    state_dir: Path,
    plugin_id: str,
) -> tuple[HostPlugin, ...]:
    """The recorded generation's host input for one id, or nothing when the id is disabled."""
    identities = payload.get("plugins")
    snapshot_plugins = snapshot.get("plugins")
    if not isinstance(identities, Mapping) or not isinstance(snapshot_plugins, Mapping):
        return ()
    entry = snapshot_plugins.get(plugin_id)
    identity = identities.get(plugin_id)
    if not isinstance(entry, Mapping) or entry.get("enabled") is not True or not isinstance(identity, Mapping):
        return ()
    artifact = identity.get("artifact")
    config = entry.get("config")
    caps = entry.get("capabilities")
    if not isinstance(artifact, str) or not artifact or not isinstance(config, Mapping):
        raise PreflightRefused("revision_payload_invalid", f"recorded identity for {plugin_id} is malformed")
    if not isinstance(caps, list) or not all(isinstance(item, str) for item in caps):
        raise PreflightRefused("revision_payload_invalid", f"recorded capabilities for {plugin_id} must be a list of strings")
    return (
        HostPlugin(
            root=state_dir / "artifacts" / artifact,
            manifest=manifests[plugin_id],
            config=json_wire_object(config),
            capabilities=frozenset(caps),
        ),
    )


def verify_preflight(
    *,
    database: Path,
    workspace: str,
    state_dir: Path,
    candidate_package_root: Path,
    candidate_sdk_sha256: str,
    candidate_sdk_protocol: int,
    candidate_launch_protocol: int,
    image_digest: str,
    web_bundle: str | None,
    target_schema: int = SCHEMA_VERSION,
) -> PreflightReport:
    """Run every preflight check for one candidate and return the report, or raise a refusal."""
    connection = read_connection(database)
    try:
        plan = schema_plan(connection, target=target_schema)
        revision_id, payload = read_workspace_revision(connection, workspace)
        snapshot, manifests = verify_recorded_generation(state_dir, revision_id, payload)
        rows = read_plugin_state(connection)
        state_report = verify_plugin_state(rows, manifests)
        image_changed, bundled_changes = verify_generation_diff(
            payload,
            snapshot,
            manifests,
            candidate_package_root=candidate_package_root,
            image_digest=image_digest,
            candidate_sdk_protocol=candidate_sdk_protocol,
            candidate_launch_protocol=candidate_launch_protocol,
        )
        session_ids = [
            _as_str(row["session_id"])
            for row in connection.execute("SELECT session_id FROM sessions WHERE workspace = ? ORDER BY session_id ASC", (workspace,)).fetchall()
        ]
    finally:
        connection.close()
    dry_run = verify_dry_run(
        state_dir=state_dir,
        workspace=Path(workspace),
        payload=payload,
        snapshot=snapshot,
        manifests=manifests,
        rows=rows,
        session_ids=session_ids,
        candidate_package_root=candidate_package_root,
        image_digest=image_digest,
    )
    return PreflightReport(
        schema=plan,
        recorded_revision_id=revision_id,
        recorded_sdk_sha256=_as_str(payload.get("sdk_sha256", "")),
        candidate_sdk_sha256=candidate_sdk_sha256,
        recorded_sdk_protocol=_as_int(payload.get("sdk_protocol", 0)),
        candidate_sdk_protocol=candidate_sdk_protocol,
        candidate_launch_protocol=candidate_launch_protocol,
        image_changed=image_changed,
        bundled_changes=bundled_changes,
        migrated_owners=dry_run.migrated_owners,
        deactivated_owners=dry_run.deactivated_owners,
        plugin_state=state_report,
        image_digest=image_digest,
        web_bundle=web_bundle,
    )


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PreflightRefused("plugin_state_invalid", "a plugin state row is malformed")
    return value


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer field")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError(f"not an integer field: {value!r}")
