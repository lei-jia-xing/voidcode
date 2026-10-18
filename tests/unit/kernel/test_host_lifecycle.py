"""Host protocol coverage for the pure lifecycle transforms activation relies on."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import write_reload_plugin
from voidcode.kernel.host import HostClient, HostUnavailable
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import HostPlugin

PLUGIN_ID = "reload-fixture"
CAPABILITIES = frozenset({"workspace.write"})


def _host_plugin(source: Path) -> HostPlugin:
    manifest = parse_plugin_manifest(json.loads((source / "voidcode-plugin.json").read_text(encoding="utf-8")))
    return HostPlugin(root=source, manifest=manifest, config={}, capabilities=CAPABILITIES)


def test_migration_and_deactivation_transforms_run_in_the_host(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", state_schema_version=2, migration="ok", deactivation="pause")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with HostClient((_host_plugin(source),), workspace=workspace, revision_id="rev-lifecycle") as client:
        migrated = client.migrate_owner_state(PLUGIN_ID, {"note": "v1"}, from_version=1, to_version=2)
        assert migrated == {"note": "v1", "migrated_to": 2}

        paused = client.prepare_deactivate_owner_state(PLUGIN_ID, {"note": "v1"}, scope="session", scope_key="sess-1", tip_sequence=7)
        assert paused == {"note": "v1", "paused": True, "tip": 7}
        workspace_copy = client.prepare_deactivate_owner_state(
            PLUGIN_ID, {"note": "v1"}, scope="workspace", scope_key=str(workspace), tip_sequence=None
        )
        assert workspace_copy == {"note": "v1", "paused": True, "tip": None}

        # A workspace-scope row has no single session tip, and a session row must have one.
        with pytest.raises(RuntimeError, match="session-scope deactivation requires"):
            client.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="session", scope_key="sess-1", tip_sequence=None)
        with pytest.raises(RuntimeError, match="no single session event tip"):
            client.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="workspace", scope_key=str(workspace), tip_sequence=3)


def test_unsupported_and_failing_transforms_are_visible(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_reload_plugin(plain, tool_output="v1")
    failing = tmp_path / "failing"
    write_reload_plugin(failing, tool_output="v1", migration="raise", deactivation="raise")
    refusing = tmp_path / "refusing"
    write_reload_plugin(refusing, tool_output="v1", migration="refuse")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with HostClient((_host_plugin(plain),), workspace=workspace, revision_id="rev-plain") as client:
        with pytest.raises(RuntimeError, match="declares no migrate_state transform"):
            client.migrate_owner_state(PLUGIN_ID, {}, from_version=1, to_version=2)
        with pytest.raises(RuntimeError, match="declares no prepare_deactivate transform"):
            client.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="session", scope_key="s", tip_sequence=1)
    with HostClient((_host_plugin(failing),), workspace=workspace, revision_id="rev-failing") as client:
        # An unexpected exception is redacted on the wire; a ValueError is the owner's own refusal text.
        with pytest.raises(RuntimeError, match="state migration failed: RuntimeError"):
            client.migrate_owner_state(PLUGIN_ID, {}, from_version=1, to_version=2)
        with pytest.raises(RuntimeError, match="state deactivation failed: RuntimeError"):
            client.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="session", scope_key="s", tip_sequence=1)
    with HostClient((_host_plugin(refusing),), workspace=workspace, revision_id="rev-refusing") as client:
        with pytest.raises(RuntimeError, match="candidate refuses to migrate this state"):
            client.migrate_owner_state(PLUGIN_ID, {}, from_version=1, to_version=2)


def test_lifecycle_hook_arity_is_enforced_before_activation(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", migration="ok")
    module = source / "reload_plugin_impl.py"
    module.write_text(
        module.read_text(encoding="utf-8").replace("def migrate_state(state, from_version, to_version):", "def migrate_state(state, to_version):"),
        encoding="utf-8",
    )
    manifest = json.loads((source / "voidcode-plugin.json").read_text(encoding="utf-8"))
    digests = {entry["path"]: entry for entry in manifest["files"]}
    digests["reload_plugin_impl.py"] = {
        "path": "reload_plugin_impl.py",
        "sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
    }
    manifest["files"] = [digests["__init__.py"], digests["reload_plugin_impl.py"]]
    (source / "voidcode-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(HostUnavailable, match="migrate_state callback"):
        with HostClient((_host_plugin(source),), workspace=workspace, revision_id="rev-arity"):
            pass
