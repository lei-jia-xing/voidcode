"""Runtime coverage for the pure lifecycle transforms activation relies on."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import write_reload_plugin
from voidcode.core.plugins import PluginRuntime, PluginSource

PLUGIN_ID = "reload-fixture"


def _source(path: Path) -> PluginSource:
    return PluginSource(root=path, plugin_id=PLUGIN_ID, config={})


def test_deactivation_transforms_run_in_the_runtime(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", deactivation="pause")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with PluginRuntime((_source(source),), workspace=workspace, revision_id="rev-lifecycle") as runtime:
        paused = runtime.prepare_deactivate_owner_state(PLUGIN_ID, {"note": "v1"}, scope="session", scope_key="sess-1", tip_sequence=7)
        assert paused == {"note": "v1", "paused": True, "tip": 7}
        workspace_copy = runtime.prepare_deactivate_owner_state(
            PLUGIN_ID, {"note": "v1"}, scope="workspace", scope_key=str(workspace), tip_sequence=None
        )
        assert workspace_copy == {"note": "v1", "paused": True, "tip": None}

        # A workspace-scope row has no single session tip, and a session row must have one.
        with pytest.raises(RuntimeError, match="session-scope deactivation requires"):
            runtime.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="session", scope_key="sess-1", tip_sequence=None)
        with pytest.raises(RuntimeError, match="no single session event tip"):
            runtime.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="workspace", scope_key=str(workspace), tip_sequence=3)


def test_unsupported_and_failing_transforms_are_visible(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write_reload_plugin(plain, tool_output="v1")
    failing = tmp_path / "failing"
    write_reload_plugin(failing, tool_output="v1", deactivation="raise")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with PluginRuntime((_source(plain),), workspace=workspace, revision_id="rev-plain") as runtime:
        with pytest.raises(RuntimeError, match="declares no prepare_deactivate transform"):
            runtime.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="session", scope_key="s", tip_sequence=1)
    with PluginRuntime((_source(failing),), workspace=workspace, revision_id="rev-failing") as runtime:
        with pytest.raises(RuntimeError, match="state deactivation"):
            runtime.prepare_deactivate_owner_state(PLUGIN_ID, {}, scope="session", scope_key="s", tip_sequence=1)


def test_lifecycle_hook_arity_is_enforced_before_activation(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", deactivation="pause")
    module = source / "__init__.py"
    old = "def prepare_deactivate(state, scope, scope_key, tip_sequence):"
    module.write_text(module.read_text(encoding="utf-8").replace(old, "def prepare_deactivate(state, scope):"), encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="prepare_deactivate callback"):
        with PluginRuntime((_source(source),), workspace=workspace, revision_id="rev-arity"):
            pass
