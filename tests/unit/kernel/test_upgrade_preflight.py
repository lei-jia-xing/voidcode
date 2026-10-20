"""Behavior tests for candidate preflight: read-only checks plus the throwaway dry-run.

Each case builds a real kernel database through the real store, with real pinned artifacts from a
real fixture plugin source. Refusals must be named, the live database and artifacts must be
untouched by every check, and the dry-run must load the target generation and prove its state
copies inside scratch hosts that leave nothing behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import write_reload_plugin
from voidcode.kernel import __main__ as kernel_cli
from voidcode.kernel.activation import write_private_snapshot
from voidcode.kernel.images import (
    IMAGE_DIGEST_ENV,
    IMAGE_ROOT_ENV,
    IMAGES_DIR_NAME,
    Image,
    materialize,
    running_package_root,
)
from voidcode.kernel.preflight import (
    SUPPORTED_TRANSITIONS,
    BundledChange,
    PreflightRefused,
    PreflightReport,
    read_connection,
    read_workspace_revision,
    schema_plan,
    verify_generation_diff,
    verify_plugin_state,
    verify_preflight,
    verify_recorded_generation,
)
from voidcode.kernel.storage.store import SCHEMA_MAGIC, SCHEMA_VERSION, SqliteStore, canonical_json
from voidcode.sdk.manifest import MANIFEST_FILE_NAME, PluginManifest, parse_plugin_manifest

WORKSPACE = "/tmp/preflight-workspace"
PLUGIN_ID = "probe-plugin"
STATE_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}
PERMISSIVE_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
}
MIGRATED_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {"count": {"type": "integer"}, "migrated_to": {"type": "integer"}},
    "required": ["count", "migrated_to"],
    "additionalProperties": False,
}


def _fixture_source(root: Path, **overrides: object) -> tuple[Path, PluginManifest]:
    """A real load-bearing plugin source, defaulting to generation v1 of the probe id."""
    arguments: dict[str, object] = {
        "plugin_id": PLUGIN_ID,
        "version": "1.0.0",
        "tool_output": "v1",
        "state_schema_version": 1,
        "state_schema": dict(STATE_SCHEMA),
    }
    arguments.update(overrides)
    source = write_reload_plugin(root / "plugin-src", **arguments)  # type: ignore[call-arg]
    manifest = parse_plugin_manifest(json.loads((source / MANIFEST_FILE_NAME).read_text(encoding="utf-8")))
    return source, manifest


def _artifact(state_dir: Path, source: Path, manifest: PluginManifest) -> str:
    """Pin one fixture source into live artifacts, the same bytes admission would pin."""
    identity = hashlib.sha256(canonical_json(manifest.as_payload()).encode("utf-8")).hexdigest()
    root = state_dir / "artifacts" / identity
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_FILE_NAME).write_text(canonical_json(manifest.as_payload()), encoding="utf-8")
    for item in manifest.files:
        data = (source / item.path).read_bytes()
        target = root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return identity


def _snapshot() -> dict[str, object]:
    return {
        "version": 1,
        "providers": {},
        "model": {"model": None, "reasoning_effort": None},
        "policy": {"approval_mode": "ask", "read_only": False},
        "plugins": {
            PLUGIN_ID: {
                "enabled": True,
                "external": False,
                "path": None,
                "canonical_path": "/tmp/source",
                "trusted": True,
                "capabilities": ["workspace.write"],
                "entry_config": {},
                "config": {},
            }
        },
        "authorized_paths": [],
    }


def _state(
    root: Path,
    *,
    image_digest: str = "image-current",
    image_protocol: int = 1,
    sdk_sha256: str = "sdk-current",
    workspace: str = WORKSPACE,
    fixture: Mapping[str, object] | None = None,
) -> tuple[Path, Path, str, PluginManifest]:
    """Write a real database, artifact and private snapshot for one fixture generation."""
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / "kernel-v1.sqlite3"
    source, manifest = _fixture_source(root, **(fixture or {}))
    artifact = _artifact(state_dir, source, manifest)
    snapshot = _snapshot()
    payload: dict[str, object] = {
        "sdk_protocol": 1,
        "sdk_sha256": sdk_sha256,
        "python": "cpython-312",
        "dependencies": {},
        "image": {"digest": image_digest, "kernel_version": "0.1", "launch_protocol": image_protocol},
        "plugins": {PLUGIN_ID: {"manifest": manifest.as_payload(), "artifact": artifact}},
        "private_config_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    }
    with SqliteStore(database) as store:
        revision_id = store.put_revision(payload)
        store.record_workspace_revision(workspace, revision_id)
    write_private_snapshot(state_dir, revision_id, snapshot)
    with sqlite3.connect(database) as raw:
        # Plugin state rows live in a session scope, so the fixture owns a real idle session.
        raw.execute(
            """
            INSERT INTO sessions (
                session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at,
                last_event_sequence, parent_session_id, root_session_id, depth, tool_allowlist_json
            ) VALUES ('session-1', ?, 'idle', ?, NULL, 0, 1, 1, 0, NULL, 'session-1', 0, NULL)
            """,
            (workspace, revision_id),
        )
        raw.commit()
    return state_dir, database, revision_id, manifest


def _candidate(root: Path, *, fixture: Mapping[str, object] | None = None, absent: bool = False) -> Path:
    """A candidate installation layout carrying (or deliberately missing) the bundled source."""
    package_root = root / "candidate" / "voidcode"
    if absent:
        package_root.mkdir(parents=True, exist_ok=True)
        return package_root
    source, _ = _fixture_source(root / "candidate-src", **(fixture or {}))
    destination = package_root / "plugins" / PLUGIN_ID.replace("-", "_")
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        if item.is_file():
            (destination / item.name).write_bytes(item.read_bytes())
    return package_root


def _read(database: Path) -> tuple[str, Mapping[str, object]]:
    connection = read_connection(database)
    try:
        return read_workspace_revision(connection, WORKSPACE)
    finally:
        connection.close()


def _preflight(
    *,
    database: Path,
    state_dir: Path,
    candidate: Path,
    sdk_sha256: str = "sdk-current",
    sdk_protocol: int = 1,
    launch_protocol: int = 1,
    image_digest: str = "image-current",
    target_schema: int | None = None,
    workspace: str = WORKSPACE,
) -> PreflightReport:
    arguments: dict[str, object] = {
        "database": database,
        "workspace": workspace,
        "state_dir": state_dir,
        "candidate_package_root": candidate,
        "candidate_sdk_sha256": sdk_sha256,
        "candidate_sdk_protocol": sdk_protocol,
        "candidate_launch_protocol": launch_protocol,
        "image_digest": image_digest,
        "web_bundle": "bundle-digest",
    }
    if target_schema is not None:
        arguments["target_schema"] = target_schema
    return verify_preflight(**arguments)  # type: ignore[arg-type]


def _artifact_dir(state_dir: Path, payload: Mapping[str, object]) -> Path:
    plugins = payload["plugins"]
    assert isinstance(plugins, Mapping)
    identity = plugins[PLUGIN_ID]
    assert isinstance(identity, Mapping)
    artifact = identity["artifact"]
    assert isinstance(artifact, str)
    return state_dir / "artifacts" / artifact


def _insert_state(database: Path, *, schema_version: int, payload: Mapping[str, object]) -> None:
    with sqlite3.connect(database) as raw:
        raw.execute(
            """
            INSERT INTO plugin_state (scope, scope_key, owner, version, schema_version, revision_id, payload_json, updated_at)
            VALUES ('session', 'session-1', ?, 1, ?, 'revision', ?, 1)
            """,
            (PLUGIN_ID, schema_version, canonical_json(payload)),
        )
        raw.commit()


def _read_state_row(database: Path) -> Mapping[str, object]:
    with sqlite3.connect(database) as raw:
        row = raw.execute("SELECT payload_json, version, schema_version FROM plugin_state WHERE owner = ?", (PLUGIN_ID,)).fetchone()
    return {"payload": json.loads(str(row[0])), "version": int(row[1]), "schema_version": int(row[2])}


def _dump_logical_content(database: Path) -> tuple[str, ...]:
    """Every committed row, as text: logical content without file-layout bytes."""
    with sqlite3.connect(database) as raw:
        return tuple(raw.iterdump())


def _live_fingerprint(state_dir: Path) -> tuple[object, ...]:
    """Content-level proof that nothing live changed: every durable file outside scratch.

    Bytecode caches are excluded the same way the image digest excludes them, and the WAL
    shared-memory sidecar is excluded because it is rebuilt process-shared memory rather than
    durable state; the main database file and the WAL itself are compared byte for byte.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(state_dir)
        if "__pycache__" in relative.parts or relative.parts[0] == "kernel-images" or relative.suffix == ".sqlite3-shm":
            continue
        entries.append((relative.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(entries)


def test_schema_plan_accepts_equal_and_declared_transition_and_refuses_the_rest(tmp_path: Path) -> None:
    _, database, _, _ = _state(tmp_path)
    connection = read_connection(database)
    try:
        plan = schema_plan(connection)
        assert plan.recorded == SCHEMA_VERSION and plan.transition is None
        next_version = plan.recorded + 1
        declared = SUPPORTED_TRANSITIONS.get((plan.recorded, next_version))
        if declared is None:
            with pytest.raises(PreflightRefused) as unsupported_next:
                schema_plan(connection, target=next_version)
            assert unsupported_next.value.reason == "db_schema_unsupported_transition"
        else:
            assert schema_plan(connection, target=next_version).transition == declared
        with pytest.raises(PreflightRefused) as newer:
            schema_plan(connection, target=plan.recorded - 1)
        assert newer.value.reason == "db_schema_newer"
        with pytest.raises(PreflightRefused) as unsupported:
            schema_plan(connection, target=plan.recorded + 99)
        assert unsupported.value.reason == "db_schema_unsupported_transition"
    finally:
        connection.close()

    foreign_magic = tmp_path / "foreign-magic.sqlite3"
    with sqlite3.connect(foreign_magic) as raw:
        raw.execute("CREATE TABLE kernel_identity (id INTEGER PRIMARY KEY CHECK (id = 1), magic TEXT NOT NULL, schema_version INTEGER NOT NULL)")
        raw.execute("INSERT INTO kernel_identity VALUES (1, 'other-magic', ?)", (plan.recorded,))
        raw.commit()
    with pytest.raises(PreflightRefused) as foreign:
        read_connection(foreign_magic)
    assert foreign.value.reason == "db_magic"


def test_read_connection_refuses_writes_and_the_file_stays_untouched(tmp_path: Path) -> None:
    """With the old kernel connected (the production topology), preflight changes no file bytes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = str(workspace.resolve())
    state_dir, database, _, _ = _state(tmp_path, workspace=resolved)
    before = _live_fingerprint(state_dir)

    connection = read_connection(database)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO revisions (revision_id, created_at, payload_json) VALUES ('x', 1, '{}')")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE workspace_state SET active_revision_id = 'x'")
    finally:
        connection.close()

    holder = SqliteStore(database)
    try:
        report = _preflight(database=database, state_dir=state_dir, candidate=_candidate(tmp_path), workspace=resolved)
        assert _live_fingerprint(state_dir) == before, "preflight must not touch live files"
    finally:
        holder.close()
    assert report.as_payload()["ok"] is True
    scratch = state_dir / IMAGES_DIR_NAME / "image-current" / "scratch"
    if scratch.is_dir():
        assert list(scratch.iterdir()) == [], "the dry-run scratch tree must be removed afterwards"


def test_preflight_is_logically_a_noop_on_a_quiet_database(tmp_path: Path) -> None:
    """With no writer connected, SQLite may finalize WAL bookkeeping bytes on a lone open.

    That moves already-committed frames and can neither lose nor invent a fact; the proof is that
    every committed row is identical before and after. (The byte-identity proof above holds with
    the old kernel connected, which is the topology preflight runs in.)
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = str(workspace.resolve())
    state_dir, database, _, _ = _state(tmp_path, workspace=resolved)
    _insert_state(database, schema_version=1, payload={"count": 3})
    before = _dump_logical_content(database)
    _preflight(database=database, state_dir=state_dir, candidate=_candidate(tmp_path), workspace=resolved)
    assert _dump_logical_content(database) == before


def test_recorded_generation_verification_refuses_missing_or_modified_bytes(tmp_path: Path) -> None:
    state_dir, database, _, _ = _state(tmp_path)
    revision_id, payload = _read(database)
    snapshot, manifests = verify_recorded_generation(state_dir, revision_id, payload)
    assert set(manifests) == {PLUGIN_ID}
    plugins = snapshot["plugins"]
    assert isinstance(plugins, Mapping) and plugins[PLUGIN_ID]["enabled"] is True

    module = next(path for path in _artifact_dir(state_dir, payload).iterdir() if path.suffix == ".py" and path.name != "__init__.py")
    original = module.read_bytes()
    module.write_bytes(original + b"\n# tampered\n")
    with pytest.raises(PreflightRefused) as modified:
        verify_recorded_generation(state_dir, revision_id, payload)
    assert modified.value.reason == "artifact_modified"

    module.unlink()
    with pytest.raises(PreflightRefused) as missing_file:
        verify_recorded_generation(state_dir, revision_id, payload)
    assert missing_file.value.reason in {"artifact_modified", "artifact_missing"}

    module.write_bytes(original)
    (state_dir / "private" / f"{revision_id}.json").write_text(json.dumps({"version": 1, "plugins": {}}), encoding="utf-8")
    with pytest.raises(PreflightRefused) as mismatch:
        verify_recorded_generation(state_dir, revision_id, payload)
    assert mismatch.value.reason == "snapshot_digest_mismatch"


def _state_report(root: Path, *, schema_version: int, state_payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    from voidcode.kernel.preflight import read_plugin_state as _read_state

    state_dir, database, _, _ = _state(root)
    revision_id, revision_payload = _read_from(database)
    _, manifests = verify_recorded_generation(state_dir, revision_id, revision_payload)
    _insert_state(database, schema_version=schema_version, payload=state_payload)
    connection = read_connection(database)
    try:
        rows = _read_state(connection)
    finally:
        connection.close()
    return verify_plugin_state(rows, manifests)


def _read_from(database: Path) -> tuple[str, Mapping[str, object]]:
    connection = read_connection(database)
    try:
        return read_workspace_revision(connection, WORKSPACE)
    finally:
        connection.close()


def test_empty_generation_passes_with_nothing_to_verify_or_load(tmp_path: Path) -> None:
    """All plugins disabled is a legal generation: the dry-run loads an empty host and plans nothing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = str(workspace.resolve())
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / "kernel-v1.sqlite3"
    snapshot = {
        "version": 1,
        "providers": {},
        "model": {"model": None, "reasoning_effort": None},
        "policy": {"approval_mode": "ask", "read_only": False},
        "plugins": {PLUGIN_ID: {"enabled": False, "external": False, "path": None, "canonical_path": "/tmp/source"}},
        "authorized_paths": [],
    }
    import hashlib as _hashlib

    from voidcode.kernel.storage.store import SqliteStore as _Store

    payload: dict[str, object] = {
        "sdk_protocol": 1,
        "sdk_sha256": "sdk-current",
        "python": "cpython-312",
        "dependencies": {},
        "image": {"digest": "image-current", "kernel_version": "0.1", "launch_protocol": 1},
        "plugins": {},
        "private_config_sha256": _hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    }
    with _Store(database) as store:
        revision_id = store.put_revision(payload)
        store.record_workspace_revision(resolved, revision_id)
    write_private_snapshot(state_dir, revision_id, snapshot)
    report = _preflight(database=database, state_dir=state_dir, candidate=_candidate(tmp_path), workspace=resolved)
    assert report.as_payload()["ok"] is True
    assert report.migrated_owners == () and report.deactivated_owners == ()


def test_plugin_state_rows_are_checked_against_the_pinned_manifest(tmp_path: Path) -> None:
    validated = _state_report(tmp_path / "valid", schema_version=1, state_payload={"count": 3})
    assert validated[0]["validated"] is True

    with pytest.raises(PreflightRefused) as invalid:
        _state_report(tmp_path / "invalid", schema_version=1, state_payload={"count": "not-an-integer"})
    assert invalid.value.reason == "plugin_state_invalid"

    with pytest.raises(PreflightRefused) as downgrade:
        _state_report(tmp_path / "downgrade", schema_version=2, state_payload={"count": 3})
    assert downgrade.value.reason == "plugin_state_downgrade"


def test_generation_diff_recognizes_bundled_and_identity_changes(tmp_path: Path) -> None:
    state_dir, database, _, _ = _state(tmp_path)
    revision_id, payload = _read(database)
    snapshot, manifests = verify_recorded_generation(state_dir, revision_id, payload)

    def diff(*, candidate: Path, image_digest: str = "image-current", sdk_protocol: int = 1) -> tuple[bool, tuple[BundledChange, ...]]:
        return verify_generation_diff(
            payload,
            snapshot,
            manifests,
            candidate_package_root=candidate,
            image_digest=image_digest,
            candidate_sdk_protocol=sdk_protocol,
            candidate_launch_protocol=1,
        )

    assert diff(candidate=_candidate(tmp_path)) == (False, ())

    changed = diff(candidate=_candidate(tmp_path / "edited", fixture={"tool_output": "v2"}))[1]
    assert len(changed) == 1
    assert changed[0].plugin_id == PLUGIN_ID
    assert changed[0].recorded_artifact != changed[0].candidate_artifact

    missing = diff(candidate=_candidate(tmp_path / "absent", absent=True))[1]
    assert missing[0].candidate_artifact is None

    assert diff(candidate=_candidate(tmp_path), image_digest="image-next")[0] is True

    with pytest.raises(PreflightRefused) as sdk_moved:
        diff(candidate=_candidate(tmp_path), sdk_protocol=2)
    assert sdk_moved.value.reason == "host_protocol_mismatch"


def test_generation_diff_refuses_an_unsupported_launch_protocol(tmp_path: Path) -> None:
    state_dir, database, _, _ = _state(tmp_path, image_protocol=9)
    revision_id, payload = _read(database)
    snapshot, manifests = verify_recorded_generation(state_dir, revision_id, payload)
    with pytest.raises(PreflightRefused) as refused:
        verify_generation_diff(
            payload,
            snapshot,
            manifests,
            candidate_package_root=_candidate(tmp_path),
            image_digest="image-current",
            candidate_sdk_protocol=1,
            candidate_launch_protocol=1,
        )
    assert refused.value.reason == "launch_protocol_mismatch"


def _full(
    tmp_path: Path,
    *,
    fixture: Mapping[str, object] | None = None,
    candidate_fixture: Mapping[str, object] | None = None,
    candidate_absent: bool = False,
    state_payload: Mapping[str, object] | None = None,
    state_schema_version: int = 1,
) -> tuple[Path, Path, PreflightReport]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = str(workspace.resolve())
    state_dir, database, _, _ = _state(tmp_path, fixture=fixture, workspace=resolved)
    if state_payload is not None:
        _insert_state(database, schema_version=state_schema_version, payload=state_payload)
    candidate = _candidate(tmp_path, fixture=candidate_fixture, absent=candidate_absent)
    report = _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved)
    return database, state_dir, report


def test_dry_run_loads_an_unchanged_generation_and_plans_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = str(workspace.resolve())
    state_dir, database, _, _ = _state(tmp_path, workspace=resolved)
    _insert_state(database, schema_version=1, payload={"count": 3})
    candidate = _candidate(tmp_path)
    holder = SqliteStore(database)
    try:
        before = _live_fingerprint(state_dir)
        report = _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved)
        assert _live_fingerprint(state_dir) == before
    finally:
        holder.close()
    assert report.migrated_owners == () and report.deactivated_owners == ()
    assert report.adoption_required is False


def test_dry_run_proves_a_migration_without_writing_it(tmp_path: Path) -> None:
    v2 = {"tool_output": "v2", "version": "2.0.0", "state_schema_version": 2, "state_schema": MIGRATED_SCHEMA, "migration": "ok"}
    database, state_dir, report = _full(tmp_path, candidate_fixture=v2, state_payload={"count": 3})

    assert report.migrated_owners == (PLUGIN_ID,)
    assert report.deactivated_owners == ()
    assert report.adoption_required is True
    row = _read_state_row(database)
    assert row["payload"] == {"count": 3} and row["version"] == 1 and row["schema_version"] == 1


def test_dry_run_refuses_a_broken_migration(tmp_path: Path) -> None:
    broken = {"tool_output": "v2", "version": "2.0.0", "state_schema_version": 2, "state_schema": MIGRATED_SCHEMA, "migration": "raise"}
    with pytest.raises(PreflightRefused) as refused:
        _full(tmp_path, candidate_fixture=broken, state_payload={"count": 3})
    assert refused.value.reason == "migration_failed"
    assert PLUGIN_ID in refused.value.detail and "1->2" in refused.value.detail


def test_dry_run_refuses_a_migration_that_violates_the_target_schema(tmp_path: Path) -> None:
    invalid = {"tool_output": "v2", "version": "2.0.0", "state_schema_version": 2, "state_schema": MIGRATED_SCHEMA, "migration": "invalid"}
    with pytest.raises(PreflightRefused) as refused:
        _full(tmp_path, candidate_fixture=invalid, state_payload={"count": 3})
    assert refused.value.reason == "migration_failed"


def test_dry_run_deactivates_a_plugin_the_candidate_dropped(tmp_path: Path) -> None:
    database, state_dir, report = _full(
        tmp_path,
        fixture={"deactivation": "pause", "state_schema": dict(PERMISSIVE_SCHEMA)},
        candidate_absent=True,
        state_payload={"count": 3},
    )
    assert report.deactivated_owners == (PLUGIN_ID,)
    assert report.migrated_owners == ()
    assert _read_state_row(database)["payload"] == {"count": 3}


def test_dry_run_refuses_a_broken_deactivation(tmp_path: Path) -> None:
    with pytest.raises(PreflightRefused) as refused:
        _full(tmp_path, fixture={"deactivation": "raise"}, candidate_absent=True, state_payload={"count": 3})
    assert refused.value.reason == "migration_failed"
    assert PLUGIN_ID in refused.value.detail and "session:session-1" in refused.value.detail


def test_dry_run_refuses_an_unloadable_candidate_generation(tmp_path: Path) -> None:
    with pytest.raises(PreflightRefused) as refused:
        _full(tmp_path, candidate_fixture={"factory_error": True})
    assert refused.value.reason == "plugin_load_failed"


def test_dry_run_refuses_an_undeclared_candidate_source(tmp_path: Path) -> None:
    package = _candidate(tmp_path)
    (package / "plugins" / PLUGIN_ID.replace("-", "_") / "stowaway.py").write_text("x = 1\n", encoding="utf-8")
    state_dir, database, _, _ = _state(tmp_path)
    with pytest.raises(PreflightRefused) as refused:
        _preflight(database=database, state_dir=state_dir, candidate=package)
    assert refused.value.reason == "admission_failed"


def test_verify_preflight_reports_a_complete_decision(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = str(workspace.resolve())
    state_dir, database, _, _ = _state(tmp_path, workspace=resolved)
    candidate = _candidate(tmp_path)
    _insert_state(database, schema_version=1, payload={"count": 3})

    identical = _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved)
    payload = identical.as_payload()
    assert payload["adoption_required"] is False
    assert payload["recorded_revision_id"] and payload["image_digest"] == "image-current"
    assert payload["migrated_owners"] == [] and payload["deactivated_owners"] == []
    plugin_state = payload["plugin_state"]
    assert isinstance(plugin_state, list) and plugin_state[0]["validated"] is True

    if (SCHEMA_VERSION, SCHEMA_VERSION + 1) in SUPPORTED_TRANSITIONS:
        transition = _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved, target_schema=SCHEMA_VERSION + 1)
        assert transition.as_payload()["transition"] == SUPPORTED_TRANSITIONS[(SCHEMA_VERSION, SCHEMA_VERSION + 1)]
        assert transition.adoption_required is True

    v2 = {"tool_output": "v2", "version": "2.0.0", "state_schema_version": 2, "state_schema": MIGRATED_SCHEMA, "migration": "ok"}
    changed = _preflight(database=database, state_dir=state_dir, candidate=_candidate(tmp_path / "edited", fixture=v2), workspace=resolved)
    assert changed.adoption_required is True
    assert changed.migrated_owners == (PLUGIN_ID,)
    bundled = changed.as_payload()["bundled_changes"]
    assert isinstance(bundled, list) and bundled[0]["plugin_id"] == PLUGIN_ID

    assert (
        _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved, sdk_sha256="sdk-next").adoption_required is True
    )
    with pytest.raises(PreflightRefused) as protocol:
        _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved, sdk_protocol=2)
    assert protocol.value.reason == "host_protocol_mismatch"
    assert (
        _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved, image_digest="image-next").adoption_required
        is True
    )

    with pytest.raises(PreflightRefused) as refusal:
        _preflight(database=database, state_dir=state_dir, candidate=candidate, workspace=resolved, target_schema=SCHEMA_VERSION + 99)
    assert refusal.value.reason == "db_schema_unsupported_transition"


def test_preflight_cli_answers_with_one_json_object(tmp_path: Path) -> None:
    """The real CLI is the surface a supervisor runs; it must work before any kernel bootstrap."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir, _, _, manifest = _state(tmp_path, workspace=str(workspace.resolve()))
    source_root = running_package_root()

    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "voidcode.kernel",
            "preflight",
            "--workspace",
            str(workspace),
            "--state-dir",
            str(state_dir),
        ],
        env={**os.environ, "PYTHONPATH": str(source_root.parent)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["db_schema"] == SCHEMA_VERSION
    assert payload["recorded_revision_id"]
    assert payload["candidate_sdk_sha256"] != payload["recorded_sdk_sha256"], "the fixture pins another SDK"
    assert payload["adoption_required"] is True
    assert manifest.plugin_id == PLUGIN_ID


def test_preflight_cli_refuses_unsupported_state_before_anything_happens(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir, database, _, _ = _state(tmp_path, workspace=str(workspace.resolve()))
    database.rename(database.with_suffix(".moved"))

    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "voidcode.kernel",
            "preflight",
            "--workspace",
            str(workspace),
            "--state-dir",
            str(state_dir),
        ],
        env={**os.environ, "PYTHONPATH": str(running_package_root().parent)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False and payload["reason"] == "db_missing"


def test_preflight_refuses_a_foreign_or_missing_database(tmp_path: Path) -> None:
    with pytest.raises(PreflightRefused) as missing:
        read_connection(tmp_path / "absent.sqlite3")
    assert missing.value.reason == "db_missing"

    thin = tmp_path / "thin.sqlite3"
    with sqlite3.connect(thin) as raw:
        raw.execute("CREATE TABLE kernel_identity (id INTEGER PRIMARY KEY, magic TEXT NOT NULL, schema_version INTEGER NOT NULL)")
        raw.execute("INSERT INTO kernel_identity VALUES (1, ?, ?)", (SCHEMA_MAGIC, SCHEMA_VERSION))
        raw.execute("CREATE TABLE workspace_state (workspace TEXT PRIMARY KEY, active_revision_id TEXT NOT NULL, updated_at INTEGER NOT NULL)")
        raw.commit()
    with pytest.raises(PreflightRefused) as matrix:
        read_connection(thin)
    assert matrix.value.reason == "db_magic"

    state_dir, database, _, _ = _state(tmp_path)
    connection = read_connection(database)
    try:
        with pytest.raises(PreflightRefused) as no_revision:
            read_workspace_revision(connection, "/tmp/definitely-not-adopted")
        assert no_revision.value.reason == "no_adopted_revision"
    finally:
        connection.close()


def _mini_install(root: Path) -> Path:
    package = root / "install" / "voidcode"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""mini installation"""\n', encoding="utf-8")
    return root / "install"


def _elected(root: Path, *, digest: str) -> tuple[Path, Path]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    state_dir, _, _, _ = _state(root, workspace=str(workspace.resolve()), image_digest=digest)
    return workspace.resolve(), state_dir


def _wire_elected(root: Path) -> tuple[Path, Path, Image]:
    """A workspace whose elected revision pins a materialized fixture image."""
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    state_dir = root / "state"
    image = materialize(_mini_install(root), state_dir / IMAGES_DIR_NAME)
    _state(root, workspace=str(workspace.resolve()), image_digest=image.digest)
    return workspace.resolve(), state_dir, image


def test_operational_commands_run_inside_the_elected_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMAGE_DIGEST_ENV, raising=False)
    workspace, state_dir, image = _wire_elected(tmp_path)

    routed = kernel_cli._elect_image_for_command("create-session", workspace, state_dir)
    assert routed is not None and routed.digest == image.digest and routed.root == image.root

    absent_workspace, absent_state = _elected(tmp_path / "absent", digest="absent-image")
    with pytest.raises(FileNotFoundError, match="elected image"):
        kernel_cli._elect_image_for_command("create-session", absent_workspace, absent_state)


def test_routing_stays_local_without_an_election(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMAGE_DIGEST_ENV, raising=False)
    workspace, state_dir, image = _wire_elected(tmp_path)

    assert kernel_cli._elect_image_for_command("show", workspace, state_dir) is None
    assert kernel_cli._elect_image_for_command("preflight", workspace, state_dir) is None

    monkeypatch.setenv(IMAGE_DIGEST_ENV, image.digest)
    assert kernel_cli._elect_image_for_command("create-session", workspace, state_dir) is None
    monkeypatch.delenv(IMAGE_DIGEST_ENV)

    monkeypatch.setenv(IMAGE_ROOT_ENV, str(image.root))
    monkeypatch.setenv(IMAGE_DIGEST_ENV, image.digest)
    monkeypatch.setattr("voidcode.kernel.images.running_package_root", lambda: image.package_root)
    assert kernel_cli._elect_image_for_command("create-session", workspace, state_dir) is None

    no_pin_workspace, no_pin_state = _elected(tmp_path / "no-pin", digest="")
    assert kernel_cli._elect_image_for_command("create-session", no_pin_workspace, no_pin_state) is None
