"""Zero-plugin core manages session and database without assuming files, shell, or model."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.config import dumps_toml
from voidcode.core.storage.store import EventDraft, SessionStatus, StagedBudgetUpdate
from voidcode.sdk.services import PluginBudget


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
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


def test_core_status_reports_counts_usage_and_all_owner_budgets_without_plugins(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    state_dir = tmp_path / "state"
    with open_core(workspace, state_dir=state_dir) as core:
        session_id = core.create_session("sess-status").session_id
        revision = core.revision_id
        core.store.commit_staged(
            session_id,
            run_id="run-status",
            revision_id=revision,
            events=(
                EventDraft("core.user_message", "client", "user", {"content": "hi"}),
                EventDraft("core.command_completed", "plugin.probe", "user", {"name": "usage", "result": {"status": "ok"}}),
            ),
            status=SessionStatus.IDLE,
            staged_budgets=(
                StagedBudgetUpdate(owner="goal", action="set", budget=PluginBudget(owner="goal", max_total_tokens=100)),
                StagedBudgetUpdate(owner="probe", action="set", budget=PluginBudget(owner="probe", max_total_tokens=50)),
            ),
        )
        result = core.invoke_command(session_id, "status", {}, expected_revision=revision)
        assert result.status == "ok"
        assert result.data is not None
        assert result.data["event_counts"] == {"core.command_completed": 1, "core.user_message": 1}
        assert result.data["usage"]["turn_count"] == 0
        assert {item["owner"]: item["max_total_tokens"] for item in result.data["budgets"]} == {"goal": 100, "probe": 50}
        assert "goal" in (result.output or "") and "core.user_message: 1" in (result.output or "")
        with pytest.raises(ValueError, match="Unknown command"):
            core.invoke_command(session_id, "session_status", {}, expected_revision=revision)


def test_core_status_stays_read_only_direct_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.toml").write_text(
        dumps_toml({"policy": {"approval_mode": "ask", "read_only": True}, "plugins": {name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS}}),
        encoding="utf-8",
    )
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        session_id = core.create_session("sess-ro").session_id
        before = core.store.events(session_id)
        result = core.invoke_command(session_id, "status", {}, expected_revision=core.revision_id)
        assert result.status == "ok"
        assert result.data["budgets"] == ()
        assert core.store.events(session_id) == before


def test_zero_plugin_example_disables_every_bundled_plugin() -> None:
    import tomllib

    example = Path(__file__).resolve().parents[2] / "examples/zero-plugins/voidcode.toml"
    if example.is_file():
        config = tomllib.loads(example.read_text(encoding="utf-8"))
        assert set(config["plugins"]) == set(BUNDLED_PLUGIN_IDS)
        assert all(entry == {"enabled": False} for entry in config["plugins"].values())
