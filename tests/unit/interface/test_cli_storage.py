"""Contract tests for ``voidcode storage`` and ``voidcode stats``.

Every test drives the CLI against an isolated workspace and database, so the
assertions are the observable surface: exit codes, JSON payloads on stdout,
stderr ownership, and the SQLite files left on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._cli_harness import DB_FILE_NAME, run_cli

SOURCE_FILE = "note.txt"
SOURCE_TEXT = "hello from the deterministic harness\n"


@pytest.fixture
def recorded_workspace(tmp_path: Path) -> Path:
    """A workspace with one persisted deterministic session."""
    (tmp_path / SOURCE_FILE).write_text(SOURCE_TEXT, encoding="utf-8")
    result = run_cli(
        "run",
        f"read {SOURCE_FILE}",
        "--workspace",
        str(tmp_path),
        "--session-id",
        "storage-contract",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    return tmp_path


def database_path(workspace: Path) -> Path:
    """The database ``run_cli`` isolates each workspace against."""
    return workspace / DB_FILE_NAME


def test_storage_diagnostics_reports_the_persisted_session(recorded_workspace: Path) -> None:
    result = run_cli(
        "storage",
        "diagnostics",
        "--workspace",
        str(recorded_workspace),
        cwd=recorded_workspace,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(recorded_workspace)
    storage = payload["storage"]
    assert storage["counts"]["sessions"] >= 1
    assert storage["database_exists"] is True
    assert storage["database_path"] == str(database_path(recorded_workspace))
    assert storage["connection_policy"]["journal_mode"] == "wal"
    assert storage["connection_policy"]["busy_timeout_ms"] == 5000


def test_stats_tools_reports_the_tool_that_ran(recorded_workspace: Path) -> None:
    result = run_cli(
        "stats",
        "tools",
        "--workspace",
        str(recorded_workspace),
        "--json",
        cwd=recorded_workspace,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(recorded_workspace)
    effectiveness = payload["effectiveness"]
    assert effectiveness["schema_version"] == 1
    assert effectiveness["session_count"] >= 1
    assert effectiveness["tool_call_count"] >= 1
    assert effectiveness["privacy"]["stores_arguments"] is False
    tools = {entry["tool"]: entry for entry in effectiveness["tools"]}
    assert tools["read"]["calls"] >= 1


def test_storage_prune_keep_sessions_zero_removes_the_session(recorded_workspace: Path) -> None:
    prune = run_cli(
        "storage",
        "prune",
        "--workspace",
        str(recorded_workspace),
        "--keep-sessions",
        "0",
        cwd=recorded_workspace,
    )

    assert prune.returncode == 0
    assert prune.stderr == ""
    payload = json.loads(prune.stdout)
    assert payload["workspace"] == str(recorded_workspace)
    assert payload["pruned"]["sessions"] >= 1

    diagnostics = run_cli(
        "storage",
        "diagnostics",
        "--workspace",
        str(recorded_workspace),
        cwd=recorded_workspace,
    )
    assert diagnostics.returncode == 0
    assert json.loads(diagnostics.stdout)["storage"]["counts"]["sessions"] == 0


def test_storage_reset_removes_the_database_and_sidecar_files(recorded_workspace: Path) -> None:
    database = database_path(recorded_workspace)
    assert database.exists()

    result = run_cli(
        "storage",
        "reset",
        "--workspace",
        str(recorded_workspace),
        cwd=recorded_workspace,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    storage = json.loads(result.stdout)["storage"]
    assert storage["reset"] is True
    assert str(database) in storage["removed"]
    assert not database.exists()
    assert not database.with_name(f"{database.name}-wal").exists()
    assert not database.with_name(f"{database.name}-shm").exists()


@pytest.mark.parametrize(
    ("argv", "payload_key"),
    [
        (("storage", "diagnostics"), "storage"),
        (("stats", "tools", "--json"), "effectiveness"),
        (("storage", "prune"), "pruned"),
    ],
)
def test_storage_and_stats_payloads_own_stdout(
    argv: tuple[str, ...],
    payload_key: str,
    recorded_workspace: Path,
) -> None:
    result = run_cli(
        *argv,
        "--workspace",
        str(recorded_workspace),
        cwd=recorded_workspace,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(recorded_workspace)
    assert payload_key in payload
