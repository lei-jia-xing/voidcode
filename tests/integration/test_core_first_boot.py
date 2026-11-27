"""First boot without ``voidcode.toml``: providerless core, strict everywhere else."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from voidcode.bootstrap import open_core
from voidcode.core.engine import MODEL_CONFIGURATION_GUIDANCE
from voidcode.core.storage.store import SqliteStore
from voidcode.sdk.services import ModelQueryModelUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _fresh_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, tmp_path / "state"


def test_fresh_state_without_config_opens_providerless_and_keeps_commands(tmp_path: Path) -> None:
    """No file is required on first boot, but nothing pretends a model or provider exists."""
    workspace, state = _fresh_workspace(tmp_path)
    with open_core(workspace, state_dir=state) as core:
        assert core.config.model is None
        assert not (workspace / "voidcode.toml").exists()
        revision = core.revision_id
        core.create_session("fresh-session")

        catalog = core.command_catalog().as_payload()
        names = {str(item["name"]) for item in catalog["commands"] if isinstance(item, dict)}
        assert {"run", "resume", "show", "status", "help", "core.reload"} <= names

        help_result = core.invoke_command("fresh-session", "help", {}, expected_revision=revision)
        assert help_result.status == "ok"
        assert "Available commands:" in (help_result.output or "")

        status = core.invoke_command("fresh-session", "status", {}, expected_revision=revision)
        assert status.status == "ok"
        assert "model: not configured" in (status.output or "")
        assert status.data is not None
        assert {key: status.data[key] for key in ("session_id", "status", "run_id", "revision_id", "turn", "last_event_sequence")} == {
            "session_id": "fresh-session",
            "status": "idle",
            "run_id": None,
            "revision_id": revision,
            "turn": 0,
            "last_event_sequence": 0,
        }
        assert status.data["event_counts"] == {}
        assert status.data["usage"]["turn_count"] == 0
        assert status.data["budgets"] == ()

        failed = core.run("hello", session_id="fresh-session")
        assert failed.status == "failed"
        assert failed.error == MODEL_CONFIGURATION_GUIDANCE
        with pytest.raises(ModelQueryModelUnavailable, match="no model configured"):
            core.governed_model_query("fresh-session", "hello")

        assert not (workspace / "voidcode.toml").exists()
        assert (state / "voidcode.db").is_file()
        assert list((state / "private").glob("*.json"))


def test_fresh_cli_catalog_without_config_opens(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "voidcode",
            "--workspace",
            str(workspace),
            "--state-dir",
            str(state),
            "catalog",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["commands"]
    assert not (workspace / "voidcode.toml").exists()


def test_existing_revision_restores_without_live_config_and_reload_stays_strict(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    config_path = workspace / "voidcode.toml"
    config_path.write_text("schema_version = 1\n[model]\n", encoding="utf-8")
    with open_core(workspace, state_dir=state) as core:
        revision = core.revision_id
        core.create_session("restore-session")

    config_path.unlink()
    with open_core(workspace, state_dir=state) as core:
        assert core.revision_id == revision
        core.create_session("restored-live")
        status = core.invoke_command("restored-live", "status", {}, expected_revision=revision)
        assert status.status == "ok"
        assert "model: not configured" in (status.output or "")

        reloaded = core.invoke_command("restored-live", "core.reload", {}, expected_revision=revision)
        assert reloaded.status == "error"
        assert "configuration not found" in (reloaded.error or "")
        assert core.revision_id == revision
        assert not config_path.exists()


def test_existing_database_without_a_workspace_revision_stays_strict(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    state.mkdir(parents=True)
    with SqliteStore(state / "voidcode.db") as store:
        assert store.workspace_current(str(workspace)) is None
    with pytest.raises(FileNotFoundError, match="configuration not found"):
        open_core(workspace, state_dir=state)


def test_unknown_config_field_stays_strict(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    (workspace / "voidcode.toml").write_text("schema_version = 1\nunknown = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported field: unknown"):
        open_core(workspace, state_dir=state)
