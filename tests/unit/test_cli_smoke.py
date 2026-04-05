"""Smoke tests for the CLI entrypoints."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def test_python_module_help_works() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "voidcode", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_console_script_help_works() -> None:
    result = subprocess.run(
        ["voidcode", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_sessions_resume_rejects_partial_approval_flags() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "resume",
            "demo-session",
            "--approval-decision",
            "allow",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "must be provided together" in result.stderr


def test_sessions_resume_surfaces_approval_resolution_errors_cleanly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

        setup_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "voidcode",
                "run",
                "read sample.txt",
                "--workspace",
                str(workspace),
                "--session-id",
                "demo-session",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        resume_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "voidcode",
                "sessions",
                "resume",
                "demo-session",
                "--workspace",
                str(workspace),
                "--approval-request-id",
                "wrong",
                "--approval-decision",
                "allow",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    assert setup_result.returncode == 0
    assert resume_result.returncode != 0
    assert "error:" in resume_result.stderr
    assert "approval" in resume_result.stderr.lower() or "pending" in resume_result.stderr.lower()
    assert "Traceback" not in resume_result.stderr


def test_serve_command_forwards_host_port_and_workspace() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")

    with patch.object(cli, "serve", autospec=True) as serve_mock:
        result = cli.main(
            [
                "serve",
                "--workspace",
                str(workspace),
                "--host",
                "0.0.0.0",
                "--port",
                "9000",
            ]
        )

    assert result == 0
    serve_mock.assert_called_once_with(workspace=workspace, host="0.0.0.0", port=9000)
