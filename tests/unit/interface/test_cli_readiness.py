"""Contract tests for the first-task readiness preflight.

A clean environment (no provider/model) must produce runnable remediation whose
wording comes from the runtime's provider-inspection helpers, not from CLI-local
placeholders.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.contracts import ProviderReadinessResult
from voidcode.runtime.provider_inspection import (
    guidance_for_provider_error_kind,
    missing_credentials_guidance,
    missing_model_guidance,
    unconfigured_provider_guidance,
)

from ._cli_harness import run_cli

CREDENTIALS_PATH = "providers.openai.api_key"


def preflight_payload(capsys: pytest.CaptureFixture[str], *, status: str, provider: str | None) -> dict[str, Any]:
    """Run the real preflight for one provider status and return the printed payload."""
    readiness = importlib.import_module("voidcode.cli.readiness")
    result = ProviderReadinessResult(
        provider=provider,
        model="gpt-4o" if provider is not None else None,
        configured=status != "unconfigured",
        ok=False,
        status=status,
        guidance="runtime guidance",
    )

    code = readiness.run_readiness_preflight(readiness=result, workspace=Path("/tmp/readiness-workspace"), json_output=True)

    assert code == 11
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("status", "provider", "kind", "expected_message"),
    [
        ("missing_model", None, "config_init", missing_model_guidance()),
        ("unconfigured", "openai", "provider_credentials", unconfigured_provider_guidance("openai")),
        ("missing_auth", "openai", "provider_credentials", missing_credentials_guidance("openai")),
        ("invalid_model", "openai", "provider_models", guidance_for_provider_error_kind("invalid_model")),
        ("streaming_unsupported", "openai", "provider_inspect", guidance_for_provider_error_kind("unsupported_feature")),
    ],
)
def test_every_provider_status_gets_a_remediation(
    status: str,
    provider: str | None,
    kind: str,
    expected_message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = preflight_payload(capsys, status=status, provider=provider)

    assert [action["kind"] for action in payload["actions"]] == [kind, "doctor"]
    remediation = payload["actions"][0]
    assert remediation["message"] == expected_message
    assert remediation["command"].startswith("voidcode ")
    assert "provider/model" not in remediation["command"]
    assert all(action["command"].startswith("voidcode ") for action in payload["actions"])


@pytest.mark.parametrize("status", ["unconfigured", "missing_auth"])
def test_credential_gaps_name_the_config_path(status: str, capsys: pytest.CaptureFixture[str]) -> None:
    payload = preflight_payload(capsys, status=status, provider="openai")

    assert CREDENTIALS_PATH in payload["actions"][0]["message"]
    assert payload["actions"][0]["command"].endswith("provider inspect openai --workspace /tmp/readiness-workspace")


def test_unavailable_statuses_keep_the_doctor_action_only(capsys: pytest.CaptureFixture[str]) -> None:
    payload = preflight_payload(capsys, status="ready", provider="openai")

    assert [action["kind"] for action in payload["actions"]] == ["doctor"]


def test_clean_environment_run_preflight_is_actionable(tmp_path: Path) -> None:
    result = run_cli(
        "run",
        "read note.txt",
        "--workspace",
        str(tmp_path),
        "--json",
        cwd=tmp_path,
        env={"VOIDCODE_EXECUTION_ENGINE": "provider"},
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 11
    assert payload["first_task_readiness"]["status"] == "not_ready"
    actions = payload["actions"]
    assert [action["kind"] for action in actions] == ["config_init", "doctor"]
    for action in actions:
        assert action["command"].startswith("voidcode ")
        assert "provider/model" not in action["command"]
    assert actions[0]["message"] == missing_model_guidance()


def test_unconfigured_provider_run_preflight_names_the_credentials_path(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(json.dumps({"model": "openai/gpt-4o"}), encoding="utf-8")

    result = run_cli("run", "read note.txt", "--workspace", str(tmp_path), "--json", cwd=tmp_path, env={"VOIDCODE_EXECUTION_ENGINE": "provider"})

    payload = json.loads(result.stdout)
    assert result.returncode == 11
    assert [action["kind"] for action in payload["actions"]] == ["provider_credentials", "doctor"]
    assert CREDENTIALS_PATH in payload["actions"][0]["message"]
    assert "provider/model" not in json.dumps(payload["actions"])


def test_missing_auth_provider_run_preflight_names_the_credentials_path(tmp_path: Path) -> None:
    config = {"model": "openai/gpt-4o", "providers": {"openai": {"base_url": "https://example.invalid/v1"}}}
    (tmp_path / ".voidcode.json").write_text(json.dumps(config), encoding="utf-8")

    result = run_cli("run", "read note.txt", "--workspace", str(tmp_path), "--json", cwd=tmp_path, env={"VOIDCODE_EXECUTION_ENGINE": "provider"})

    payload = json.loads(result.stdout)
    assert result.returncode == 11
    assert [action["kind"] for action in payload["actions"]] == ["provider_credentials", "doctor"]
    assert CREDENTIALS_PATH in payload["actions"][0]["message"]


def test_clean_environment_doctor_points_at_the_same_remediation(tmp_path: Path) -> None:
    result = run_cli("doctor", "--workspace", str(tmp_path), "--json", cwd=tmp_path, env={"VOIDCODE_EXECUTION_ENGINE": "provider"})

    payload = json.loads(result.stdout)
    assert result.returncode == 12
    readiness = payload["first_task_readiness"]
    assert readiness["status"] == "not_ready"
    assert "voidcode config init" in readiness["next_step"]
