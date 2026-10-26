"""Zero-plugin core manages session and database without assuming files, shell, or model."""

from __future__ import annotations

import json
from pathlib import Path

from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "plugins": {name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def test_zero_plugins_core_manages_sessions_and_database(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    state_dir = tmp_path / "state"
    with open_core(workspace, state_dir=state_dir) as core:
        assert core.plugin_runtime.plugin_ids == ()
        assert len(core.plugin_runtime.tools) == 0
        assert len(core.plugin_runtime.providers) == 0

        session = core.create_session("sess-zero-1")
        assert session.session_id == "sess-zero-1"
        assert session.status == "idle"

        sessions = core.sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-zero-1"

    # Reopening restores persisted state without plugins
    with open_core(workspace, state_dir=state_dir) as reopened:
        assert reopened.plugin_runtime.plugin_ids == ()
        assert len(reopened.plugin_runtime.tools) == 0
        sessions = reopened.sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-zero-1"


def test_zero_plugin_example_disables_every_bundled_plugin() -> None:
    example = Path(__file__).resolve().parents[2] / "examples/zero-plugins/voidcode.json"
    if example.is_file():
        config = json.loads(example.read_text(encoding="utf-8"))
        assert set(config["plugins"]) == set(BUNDLED_PLUGIN_IDS)
        assert all(entry == {"enabled": False} for entry in config["plugins"].values())
