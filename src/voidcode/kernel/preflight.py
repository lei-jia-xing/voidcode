"""Candidate preflight: everything a kernel upgrade would change, proven before downtime.

The first half of this module never executes candidate code and never writes: it opens the
database strictly read-only, verifies the recorded revision and its private snapshot, and diffs
the recorded generation against the candidate installation. The second half - the dry-run -
executes the candidate's plugin code inside a throwaway runtime: admission, state migration
transforms, and a real runtime load of the target generation. The dry-run is trusted exactly as the
upgrade ingress is (the user named this candidate on the command line), and it cannot touch live
state: its runtime is constructed with no store and no callbacks, and the database is only ever
opened through the same read-only reader as the first half.

Nothing here may emit a successful report for an incompatible generation: an unsupported protocol
refuses with its own reason instead of merely flagging a change, and the dry-run's failures are
refusals, not warnings.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..sdk.json_values import json_wire_object
from ..sdk.plugin import StateSchema
from .activation import (
    admit_live,
    bundled_sources,
    config_from_snapshot,
    load_private_snapshot,
    plan_state_copies,
    validate_providers,
    validate_state_payload,
)
from .plugins import PluginRuntime
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
    "DryRunReport",
    "PreflightRefused",
    "PreflightReport",
    "SchemaPlan",
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

#: The supervisor<->child launch interface this build speaks. A candidate that does not speak it is
#: refused before downtime; a supported conversion is never implied.
LAUNCH_PROTOCOL_VERSION = 1


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

    @property
    def unchanged(self) -> bool:
        return self.recorded == self.target


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
    candidate_launch_protocol: int
    image_changed: bool
    migrated_owners: tuple[str, ...]
    deactivated_owners: tuple[str, ...]
    plugin_state: tuple[Mapping[str, object], ...]
    image_digest: str
    web_bundle: str | None

    @property
    def adoption_required(self) -> bool:
        """Whether the candidate would build a new generation rather than restore the recorded one."""
        return not self.schema.unchanged or self.image_changed or bool(self.migrated_owners)

    def as_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "reason": None,
            "detail": "",
            "db_schema": self.schema.recorded,
            "target_schema": self.schema.target,
            "recorded_revision_id": self.recorded_revision_id,
            "launch_protocol": self.candidate_launch_protocol,
            "adoption_required": self.adoption_required,
            "image_changed": self.image_changed,
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
        return SchemaPlan(recorded=recorded, target=target)
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
) -> Mapping[str, object]:
    """Verify the recorded generation and its private snapshot against each other.

    The snapshot is the credential-bearing truth; the payload only carries its digest and the
    composition. Any shape this build cannot serve refuses here, before downtime.
    """
    try:
        snapshot = load_private_snapshot(state_dir, revision_id)
    except (OSError, ValueError) as exc:
        raise PreflightRefused("private_snapshot_invalid", str(exc)) from exc
    digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
    if payload.get("private_config_sha256") != digest:
        raise PreflightRefused("revision_payload_invalid", "private configuration snapshot does not match the revision it belongs to")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list) or not all(isinstance(plugin_id, str) and plugin_id for plugin_id in plugins):
        raise PreflightRefused("revision_payload_invalid", "recorded revision has no plugin composition")
    return snapshot


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
    states: Mapping[str, StateSchema],
) -> tuple[Mapping[str, object], ...]:
    """Check every owner state row against the state schema its candidate owner declares.

    A row above its owner's declared version is a downgrade and refuses; a row at the declared
    version must satisfy that owner's ``state_schema``; a row below it is reported and left alone
    (forward migration is proven separately by the dry-run, never by rewriting live rows here).
    """
    reported: list[Mapping[str, object]] = []
    for row in rows:
        owner = _as_str(row["owner"])
        declared_version = _as_int(row["schema_version"])
        state = states.get(owner)
        entry: dict[str, object] = {
            "owner": owner,
            "scope": _as_str(row["scope"]),
            "scope_key": _as_str(row["scope_key"]),
            "schema_version": declared_version,
        }
        if state is not None:
            target_version = state.version
            if declared_version > target_version:
                raise PreflightRefused(
                    "plugin_state_downgrade",
                    f"plugin {owner} state is at schema version {declared_version} while its owner declares {target_version}",
                )
            if declared_version == target_version:
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    raise PreflightRefused("plugin_state_invalid", f"plugin {owner} state row is malformed")
                try:
                    validate_state_payload(payload, state)
                except ValueError as exc:
                    raise PreflightRefused("plugin_state_invalid", f"plugin {owner} state failed its declared schema: {exc}") from exc
                entry["validated"] = True
            else:
                entry["migration_owed"] = target_version
        reported.append(entry)
    return tuple(reported)


def verify_generation_diff(
    payload: Mapping[str, object],
    *,
    image_digest: str,
    candidate_launch_protocol: int,
) -> bool:
    """Compare the recorded generation against the candidate installation, read-only.

    The image digest covers the whole installation tree, so a bundled plugin change is an image
    change. An unsupported launch protocol refuses outright: a generation built for another
    protocol is never "adopted" by this build.
    """
    recorded_image = payload.get("image")
    recorded_digest = recorded_image.get("digest") if isinstance(recorded_image, Mapping) else None
    recorded_launch = recorded_image.get("launch_protocol") if isinstance(recorded_image, Mapping) else None
    if recorded_launch is not None and recorded_launch != candidate_launch_protocol:
        raise PreflightRefused(
            "launch_protocol_mismatch",
            f"recorded generation speaks launch protocol {recorded_launch!r}; the candidate speaks {candidate_launch_protocol}",
        )
    return not isinstance(recorded_digest, str) or recorded_digest != image_digest


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
    snapshot: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
    session_ids: Sequence[str],
    candidate_package_root: Path,
) -> tuple[DryRunReport, tuple[Mapping[str, object], ...]]:
    """Load the target generation in a throwaway runtime and prove its state copies, discarding all.

    The candidate's plugin code executes here - that is the point of the check - but nothing can
    land in live state: the runtime is built with no store and no callbacks, and the state-copy
    plan is computed from the read-only rows and reported, never written.
    """
    try:
        config = config_from_snapshot(snapshot)
        admitted = admit_live(
            config,
            workspace=workspace,
            bundled_sources=bundled_sources(candidate_package_root),
        )
    except (OSError, PermissionError, ValueError) as exc:
        raise PreflightRefused("admission_failed", f"candidate cannot admit the recorded generation: {exc}") from exc
    try:
        candidate_runtime = PluginRuntime(admitted, workspace=workspace, revision_id="dry-run-target", store=None)
    except Exception as exc:
        raise PreflightRefused("plugin_load_failed", f"the candidate generation does not load: {exc}") from exc
    try:
        try:
            validate_providers(config, candidate_runtime.providers)
        except ValueError as exc:
            raise PreflightRefused("provider_mismatch", str(exc)) from exc
        states = candidate_runtime.state_metadata()
        reported = verify_plugin_state(rows, states)
        loaded = tuple(sorted(candidate_runtime.plugin_ids))
        view = _ReadOnlyStoreView(read_connection(state_dir / "kernel-v1.sqlite3"), rows)
        try:
            plan = plan_state_copies(
                store=view,
                workspace=str(workspace),
                session_ids=list(session_ids),
                # Adoption keeps the recorded configuration; rows move to whatever schema version
                # their owner now declares. A removed owner must have been disabled in the running
                # kernel first, so no owner loses its deactivation transform here.
                old_states=states,
                candidate_states=states,
                migrate=_migrate_with_owner(candidate_runtime),
                deactivate=_deactivation_unavailable,
            )
        except PreflightRefused:
            raise
        except _TransformFailed as exc:
            raise PreflightRefused("migration_failed", f"a state transform failed: {exc}") from exc
        except ValueError as exc:
            raise PreflightRefused("migration_failed", f"a state copy failed: {exc}") from exc
        except (TimeoutError, OSError, RuntimeError) as exc:
            raise PreflightRefused("migration_failed", f"a state transform failed: {exc}") from exc
        finally:
            view.close()
    finally:
        candidate_runtime.close()
    return DryRunReport(
        target_plugins=loaded,
        migrated_owners=plan.migrated_owners,
        deactivated_owners=plan.deactivated_owners,
    ), reported


class _TransformFailed(Exception):
    """A pure state transform failed, with the owner and direction it carried."""

    def __init__(self, owner: str, direction: str, detail: str) -> None:
        super().__init__(f"{owner} {direction}: {detail}")
        self.owner = owner
        self.direction = direction
        self.detail = detail


def _deactivation_unavailable(owner: str, payload: Mapping[str, object], scope: str, scope_key: str, tip: int | None) -> Mapping[str, object]:
    del payload, scope, scope_key, tip
    raise ValueError(f"plugin {owner} needs deactivation but the candidate no longer ships it; disable it before upgrading")


def _migrate_with_owner(candidate_runtime: PluginRuntime) -> Callable[[str, Mapping[str, object], int, int], Mapping[str, object]]:
    def run(owner: str, payload: Mapping[str, object], from_version: int, to_version: int) -> Mapping[str, object]:
        try:
            return candidate_runtime.migrate_owner_state(owner, payload, from_version=from_version, to_version=to_version)
        except Exception as exc:
            raise _TransformFailed(owner, f"{from_version}->{to_version}", str(exc)) from exc

    return run


def verify_preflight(
    *,
    database: Path,
    workspace: str,
    state_dir: Path,
    candidate_package_root: Path,
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
        snapshot = verify_recorded_generation(state_dir, revision_id, payload)
        rows = read_plugin_state(connection)
        image_changed = verify_generation_diff(
            payload,
            image_digest=image_digest,
            candidate_launch_protocol=candidate_launch_protocol,
        )
        session_ids = [
            _as_str(row["session_id"])
            for row in connection.execute("SELECT session_id FROM sessions WHERE workspace = ? ORDER BY session_id ASC", (workspace,)).fetchall()
        ]
    finally:
        connection.close()
    dry_run, state_report = verify_dry_run(
        state_dir=state_dir,
        workspace=Path(workspace),
        snapshot=snapshot,
        rows=rows,
        session_ids=session_ids,
        candidate_package_root=candidate_package_root,
    )
    return PreflightReport(
        schema=plan,
        recorded_revision_id=revision_id,
        candidate_launch_protocol=candidate_launch_protocol,
        image_changed=image_changed,
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
