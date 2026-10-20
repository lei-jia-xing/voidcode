"""Behavior test for generation adoption: the bootstrap path from recorded pins to a new revision.

Unlike the store-level adoption tests, this drives the real `adopt_generation`: recorded artifacts
are reused, the target revision pins this build, the private snapshot is written before the commit,
and the report names the transition. It is the test that catches assembly bugs (like a shadowed
configuration) before a supervised child ever runs them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.reload_plugin import write_reload_plugin
from voidcode.kernel.activation import admit_plugin, load_private_snapshot, sdk_source_hash
from voidcode.kernel.bootstrap import adopt_generation
from voidcode.kernel.images import installed_identity, running_package_root
from voidcode.kernel.storage.store import SCHEMA_VERSION, SqliteStore, canonical_json
from voidcode.sdk.manifest import MANIFEST_FILE_NAME, parse_plugin_manifest

PLUGIN_ID = "probe-plugin"
STATE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}


def _workspace(root: Path) -> tuple[Path, Path, str]:
    workspace, state_dir = root / "workspace", root / "state"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / "kernel-v1.sqlite3"
    source = write_reload_plugin(
        workspace / "ext-plugin",
        plugin_id=PLUGIN_ID,
        version="1.0.0",
        tool_output="v1",
        state_schema_version=1,
        state_schema=dict(STATE_SCHEMA),
    )
    plugin = admit_plugin(
        source,
        artifacts=state_dir / "artifacts",
        trusted=True,
        config={},
        capabilities=frozenset({"workspace.read", "workspace.write", "network"}),
    )
    manifest = parse_plugin_manifest(json.loads((source / MANIFEST_FILE_NAME).read_text(encoding="utf-8")))
    assert manifest.plugin_id == PLUGIN_ID
    snapshot: dict[str, object] = {
        "version": 1,
        "providers": {},
        "model": {"model": None, "reasoning_effort": None},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "plugins": {
            PLUGIN_ID: {
                "enabled": True,
                "external": True,
                "path": "ext-plugin",
                "canonical_path": str(source.resolve()),
                "trusted": True,
                "capabilities": ["workspace.read", "workspace.write", "network"],
                "entry_config": {},
                "config": {},
            }
        },
        "authorized_paths": [str(source.resolve())],
    }
    payload: dict[str, object] = {
        "sdk_protocol": 1,
        "sdk_sha256": "sdk-recorded",
        "python": "cpython-312",
        "dependencies": {},
        "image": {"digest": "image-recorded", "kernel_version": "0.0", "launch_protocol": 1},
        "plugins": {PLUGIN_ID: {"manifest": plugin.manifest.as_payload(), "artifact": plugin.root.name}},
        "private_config_sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
    }
    with SqliteStore(database) as store:
        revision_id = store.put_revision(payload)
        store.record_workspace_revision(str(workspace.resolve()), revision_id)
    from voidcode.kernel.activation import write_private_snapshot

    write_private_snapshot(state_dir, revision_id, snapshot)
    with sqlite3.connect(database) as raw:
        raw.execute(
            """
            INSERT INTO sessions (
                session_id, workspace, status, active_revision_id, run_id, turn, created_at, updated_at,
                last_event_sequence, parent_session_id, root_session_id, depth, tool_allowlist_json
            ) VALUES ('acting', ?, 'idle', ?, NULL, 0, 1, 1, 0, NULL, 'acting', 0, NULL)
            """,
            (str(workspace.resolve()), revision_id),
        )
        raw.commit()
    return workspace, state_dir, revision_id


def test_adopt_generation_builds_reports_and_commits_the_new_revision(tmp_path: Path) -> None:
    workspace, state_dir, old_revision = _workspace(tmp_path)
    workspace_name = str(workspace.resolve())
    report = adopt_generation(workspace=workspace, state_dir=state_dir, session_id="acting")

    assert report["from_revision_id"] == old_revision
    assert report["to_revision_id"] != old_revision
    assert report["migrated_owners"] == [] and report["deactivated_owners"] == []
    assert SCHEMA_VERSION == 5

    database = state_dir / "kernel-v1.sqlite3"
    with SqliteStore(database) as store:
        assert store.workspace_current(workspace_name) == report["to_revision_id"]
        assert store.session("acting").active_revision_id == report["to_revision_id"]
        assert store.revision(old_revision)["sdk_sha256"] == "sdk-recorded"
        payload = store.revision(str(report["to_revision_id"]))
        image = payload["image"]
        assert isinstance(image, Mapping)
        assert image["digest"] == installed_identity().digest
        assert image["launch_protocol"] == 1
        assert payload["sdk_sha256"] == sdk_source_hash(running_package_root() / "sdk")
        events = store.events("acting")
        assert [event.event_type for event in events] == ["kernel.revision_adopted"]
        snapshot = load_private_snapshot(state_dir, str(report["to_revision_id"]))
        snapshot_plugins = snapshot["plugins"]
        assert isinstance(snapshot_plugins, dict)
        assert set(snapshot_plugins) == {PLUGIN_ID}
