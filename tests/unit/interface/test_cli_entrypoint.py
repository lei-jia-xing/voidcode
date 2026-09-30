"""Contract tests for the CLI entrypoint surface.

Pins the documented command inventory, help/usage exit codes, and the
exit-code propagation of the console script.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from voidcode.cli_support import EXIT_USAGE_ERROR

from ._cli_harness import run_cli, run_cli_process

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
    "sessions": {"list", "resume", "answer", "export", "import", "debug", "undo", "revert", "entries", "checkout"},
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
    """The real ``python -m voidcode`` process answers help and version.

    This one keeps spawning a process: it is the only test that pins the module
    entrypoint itself (``__main__`` → ``raise SystemExit(main())``) rather than
    the in-process ``main(argv)`` call the other contract tests use.
    """
    help_result = run_cli_process("--help", cwd=tmp_path)
    version_result = run_cli_process("--version", cwd=tmp_path)

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


def test_back_to_back_invocations_share_no_state(tmp_path: Path) -> None:
    """Two in-process invocations each see only their own workspace, database and environment.

    ``run_cli`` no longer spawns, so the isolation a fresh process gave for free
    is reset per invocation instead: environment, working directory and both
    output streams, on top of the per-workspace database. Each result is
    asserted, and the caller's environment and cwd are asserted afterwards to
    pin that the reset really happened.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    for workspace in (first, second):
        workspace.mkdir()
        (workspace / "note.txt").write_text("note\n", encoding="utf-8")

    env_before = dict(os.environ)
    cwd_before = os.getcwd()

    first_run = run_cli("run", "read note.txt", "--workspace", str(first), "--json", cwd=first)
    second_run = run_cli("run", "read note.txt", "--workspace", str(second), "--json", cwd=second)
    second_list = run_cli("sessions", "list", "--workspace", str(second), "--json", cwd=second)
    credentialed = run_cli(
        "config",
        "show",
        "--workspace",
        str(second),
        cwd=second,
        env={"VOIDCODE_MODEL": "openai/gpt-4o", "OPENAI_API_KEY": "sk-sentinel-two-runs"},
    )
    uncredentialed = run_cli("config", "show", "--workspace", str(second), cwd=second)

    assert first_run.returncode == 0
    assert second_run.returncode == 0
    first_payload = json.loads(first_run.stdout)
    second_payload = json.loads(second_run.stdout)
    assert first_payload["workspace"] == str(first)
    assert second_payload["workspace"] == str(second)
    assert first_payload["output"] == second_payload["output"] == "Read 1 line(s) from note.txt."

    # The second workspace's database holds that run's session and nothing from the first.
    listed = json.loads(second_list.stdout)
    assert [row["session"]["id"] for row in listed["sessions"]] == [second_payload["session"]["session"]["id"]]

    # Each ``config show`` observed its own environment: the credential is only in the first.
    assert json.loads(credentialed.stdout)["provider_readiness"]["provider"] == "openai"
    assert "sk-sentinel-two-runs" not in credentialed.stdout
    assert json.loads(uncredentialed.stdout)["model"] is None

    assert dict(os.environ) == env_before
    assert os.getcwd() == cwd_before
