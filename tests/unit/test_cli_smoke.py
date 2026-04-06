"""Smoke tests for the CLI entrypoints."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
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
    config = SimpleNamespace(approval_mode="deny")

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config) as load_mock:
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
                    "--approval-mode",
                    "deny",
                ]
            )

    assert result == 0
    load_mock.assert_called_once_with(workspace, approval_mode="deny")
    serve_mock.assert_called_once_with(
        workspace=workspace,
        host="0.0.0.0",
        port=9000,
        config=config,
    )


def test_run_command_loads_config_and_forwards_it_to_runtime() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="allow")
    response = SimpleNamespace(events=(), output=None, session=object())

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config) as load_mock:
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.run.return_value = response
            result = cli.main(
                [
                    "run",
                    "read README.md",
                    "--workspace",
                    str(workspace),
                    "--approval-mode",
                    "allow",
                ]
            )

    assert result == 0
    load_mock.assert_called_once_with(workspace, approval_mode="allow")
    runtime_class.assert_called_once_with(workspace=workspace, config=config)


def test_run_command_uses_repo_local_config_to_allow_write_request() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / ".voidcode.json").write_text(
            json.dumps({"approval_mode": "allow"}),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "voidcode",
                "run",
                "write danger.txt config approved",
                "--workspace",
                str(workspace),
                "--session-id",
                "config-run-session",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        written = (workspace / "danger.txt").read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "EVENT runtime.approval_resolved" in result.stdout
    assert "decision=allow" in result.stdout
    assert written == "config approved"
