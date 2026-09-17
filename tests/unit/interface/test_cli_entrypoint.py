"""Contract tests for the CLI entrypoint surface.

Pins the documented command inventory, help/usage exit codes, and the
exit-code propagation of the console script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.cli_support import EXIT_USAGE_ERROR

from ._cli_harness import run_cli

ROOT_COMMANDS = {
    "acp",
    "agents",
    "commands",
    "config",
    "doctor",
    "mcp",
    "provider",
    "run",
    "serve",
    "sessions",
    "stats",
    "storage",
    "tasks",
    "tui",
    "web",
}

GROUP_SUBCOMMANDS = {
    "sessions": {"list", "resume", "answer", "export", "import", "debug", "undo", "revert", "unrevert"},
    "tasks": {"status", "output", "cancel", "retry", "steer", "list"},
    "storage": {"diagnostics", "prune", "reset"},
    "stats": {"tools"},
    "config": {"show", "schema", "init"},
    "provider": {"models", "inspect"},
    "commands": {"list", "show"},
    "agents": {"list"},
    "mcp": {"list"},
}

HELP_SURFACES = sorted({(name,) for name in ROOT_COMMANDS} | {(group, sub) for group, subs in GROUP_SUBCOMMANDS.items() for sub in subs})


def help_output(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    from voidcode.cli import app

    assert app.main([*argv, "--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


def listed_names(help_text: str) -> set[str]:
    section = help_text.split("Commands:", 1)[1]
    return {line.split()[0] for line in section.splitlines() if line.startswith("  ") and line.strip()}


def test_root_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    out = help_output(capsys)

    assert out.startswith("Usage: voidcode")
    assert listed_names(out) == ROOT_COMMANDS


@pytest.mark.parametrize("group", sorted(GROUP_SUBCOMMANDS))
def test_group_help_lists_every_subcommand(group: str, capsys: pytest.CaptureFixture[str]) -> None:
    out = help_output(capsys, group)

    assert out.startswith(f"Usage: voidcode {group}")
    assert listed_names(out) == GROUP_SUBCOMMANDS[group]


@pytest.mark.parametrize("surface", HELP_SURFACES, ids=[" ".join(surface) for surface in HELP_SURFACES])
def test_every_help_surface_exits_zero(surface: tuple[str, ...], capsys: pytest.CaptureFixture[str]) -> None:
    out = help_output(capsys, *surface)

    assert f"Usage: voidcode {' '.join(surface)}" in out
    assert "Options:" in out


def test_root_help_is_printed_when_no_command_is_given(capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    assert app.main([]) == 0
    assert "Usage: voidcode" in capsys.readouterr().out


def test_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    assert app.main(["definitely-not-a-command"]) == EXIT_USAGE_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No such command" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_option_is_a_usage_error(tmp_path: Path) -> None:
    result = run_cli("run", "read note.txt", "--workspace", str(tmp_path), "--definitely-not-an-option", cwd=tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Usage: voidcode run" in result.stderr
    assert "Traceback" not in result.stderr


def test_module_entrypoint_serves_help_and_version(tmp_path: Path) -> None:
    help_result = run_cli("--help", cwd=tmp_path)
    version_result = run_cli("--version", cwd=tmp_path)

    assert help_result.returncode == 0
    assert help_result.stdout.startswith("Usage: voidcode")
    assert version_result.returncode == 0
    assert version_result.stdout.strip().startswith("voidcode, version ")


def test_db_path_option_redirects_the_runtime_database(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.sqlite3"

    result = run_cli(
        "--db-path",
        str(explicit),
        "sessions",
        "list",
        "--workspace",
        str(tmp_path),
        "--json",
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert explicit.exists()
    assert not (tmp_path / ".cli-contracts.sqlite3").exists()


def test_runtime_error_reaches_the_process_exit_code(tmp_path: Path) -> None:
    result = run_cli("sessions", "debug", "missing-session", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == 12
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Traceback" not in result.stderr


def test_missing_workspace_reports_the_invalid_resource_exit_code(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    result = run_cli("agents", "list", "--workspace", str(missing), cwd=tmp_path)

    assert result.returncode == 16
    assert result.stdout == ""
    assert "error: workspace does not exist" in result.stderr


def test_machine_payloads_stay_on_stdout(tmp_path: Path) -> None:
    result = run_cli("sessions", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"workspace": str(tmp_path), "scope": "main", "sessions": []}
