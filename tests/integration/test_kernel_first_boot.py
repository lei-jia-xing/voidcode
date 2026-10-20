"""First boot without ``voidcode.json``: providerless kernel, strict everywhere else."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import MODEL_CONFIGURATION_GUIDANCE
from voidcode.kernel.storage.store import SqliteStore

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _fresh_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, tmp_path / "state"


def test_fresh_state_without_config_opens_providerless_and_keeps_commands(tmp_path: Path) -> None:
    """No file is required on first boot, but nothing pretends a model or provider exists."""
    workspace, state = _fresh_workspace(tmp_path)
    with open_kernel(workspace, state_dir=state) as kernel:
        assert kernel.config.model.model is None
        assert dict(kernel.config.providers) == {}
        assert not (workspace / "voidcode.json").exists()
        revision = kernel.revision_id
        kernel.create_session("fresh-session")

        catalog = kernel.command_catalog().as_payload()
        names = {str(item["name"]) for item in catalog["commands"] if isinstance(item, dict)}
        assert {"run", "resume", "show", "status", "help", "kernel.reload"} <= names

        help_result = kernel.invoke_command("fresh-session", "help", {}, expected_revision=revision)
        assert help_result.status == "ok"
        assert "Available commands:" in (help_result.output or "")

        status = kernel.invoke_command("fresh-session", "status", {}, expected_revision=revision)
        assert status.status == "ok"
        assert "model: not configured" in (status.output or "")
        assert status.data == kernel.show("fresh-session")

        failed = kernel.run("hello", session_id="fresh-session")
        assert failed.status == "failed"
        assert failed.error == MODEL_CONFIGURATION_GUIDANCE
        with pytest.raises(ValueError, match="no model configured"):
            kernel.governed_model_query("fresh-session", "hello")

        assert not (workspace / "voidcode.json").exists()
        assert (state / "kernel-v1.sqlite3").is_file()
        assert list((state / "private").glob("*.json"))


def test_fresh_cli_catalog_without_config_opens(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "voidcode.kernel",
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
    assert not (workspace / "voidcode.json").exists()


def test_existing_revision_restores_without_live_config_and_reload_stays_strict(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    config_path = workspace / "voidcode.json"
    config_path.write_text(json.dumps({"schema_version": 1, "model": {"model": None}}), encoding="utf-8")
    with open_kernel(workspace, state_dir=state) as kernel:
        revision = kernel.revision_id
        kernel.create_session("restore-session")

    config_path.unlink()
    with open_kernel(workspace, state_dir=state) as kernel:
        assert kernel.revision_id == revision
        kernel.create_session("restored-live")
        status = kernel.invoke_command("restored-live", "status", {}, expected_revision=revision)
        assert status.status == "ok"
        assert "model: not configured" in (status.output or "")

        reloaded = kernel.invoke_command("restored-live", "kernel.reload", {}, expected_revision=revision)
        assert reloaded.status == "error"
        assert "kernel configuration is required" in (reloaded.error or "")
        assert kernel.revision_id == revision
        assert not config_path.exists()


def test_existing_database_without_a_workspace_revision_stays_strict(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    state.mkdir(parents=True)
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        assert store.workspace_current(str(workspace)) is None
    with pytest.raises(FileNotFoundError, match="kernel configuration is required"):
        open_kernel(workspace, state_dir=state)


def test_legacy_file_still_rejected_and_trust_still_needs_a_real_config(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    (workspace / ".voidcode.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match=r"legacy \.voidcode\.json is not supported"):
        open_kernel(workspace, state_dir=state)
    assert not (state / "kernel-v1.sqlite3").exists()


def test_trust_without_config_stays_strict(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    with pytest.raises(FileNotFoundError, match="kernel configuration is required"):
        open_kernel(workspace, state_dir=state, trusted_plugins=frozenset({"counter"}))
    assert not (workspace / "voidcode.json").exists()


def test_unknown_config_field_stays_strict(tmp_path: Path) -> None:
    workspace, state = _fresh_workspace(tmp_path)
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "unknown": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported field: unknown"):
        open_kernel(workspace, state_dir=state)
