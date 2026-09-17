"""Contract tests for ``voidcode doctor``."""

from __future__ import annotations

import json
from pathlib import Path

from ._cli_harness import run_cli

SENTINEL_KEY = "sk-sentinel-doctor-value"


def test_doctor_json_reports_readiness_and_matches_the_exit_code(tmp_path: Path) -> None:
    result = run_cli("doctor", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert {"workspace", "summary", "results", "first_task_readiness", "has_errors", "is_healthy"} <= set(payload)
    assert payload["workspace"] == str(tmp_path)
    assert payload["first_task_readiness"]["status"] == "not_ready"
    assert payload["first_task_readiness"]["blockers"]
    assert payload["summary"]["total"] == len(payload["results"])
    assert result.returncode == (0 if payload["is_healthy"] else 12)
    assert result.returncode == 12


def test_doctor_json_reports_an_invalid_workspace_config(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text("{ not json", encoding="utf-8")

    result = run_cli("doctor", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == 12
    assert payload["first_task_readiness"]["details"]["workspace_config_valid"] is False
    assert payload["first_task_readiness"]["status"] == "not_ready"
    assert "runtime config error" in result.stderr
    assert "Traceback" not in result.stderr


def test_doctor_human_output_does_not_leak_credentials(tmp_path: Path) -> None:
    result = run_cli(
        "doctor",
        "--workspace",
        str(tmp_path),
        cwd=tmp_path,
        env={"DEEPSEEK_API_KEY": SENTINEL_KEY},
    )

    assert SENTINEL_KEY not in result.stdout
    assert SENTINEL_KEY not in result.stderr
    assert "Traceback" not in result.stderr


def test_doctor_fix_requires_an_explicit_model(tmp_path: Path) -> None:
    result = run_cli("doctor", "--workspace", str(tmp_path), "--fix", cwd=tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr


def test_doctor_fix_writes_the_starter_config_once(tmp_path: Path) -> None:
    first = run_cli("doctor", "--workspace", str(tmp_path), "--fix", "--model", "openai/gpt-4o", cwd=tmp_path)
    second = run_cli("doctor", "--workspace", str(tmp_path), "--fix", "--model", "openai/gpt-4o", cwd=tmp_path)

    assert first.returncode == 0
    assert (tmp_path / ".voidcode.json").exists()
    assert str(tmp_path / ".voidcode.json") in first.stdout
    assert second.returncode == 10
    assert "error:" in second.stderr
    assert "Traceback" not in second.stderr
