"""Contract tests for ``voidcode provider``.

``provider models`` and ``provider inspect`` always print their JSON payload on
stdout; neither takes ``--json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ._cli_harness import run_cli

SENTINEL_KEY = "sk-sentinel-provider-value"


def test_provider_models_reports_an_unconfigured_provider(tmp_path: Path) -> None:
    result = run_cli("provider", "models", "nope", "--workspace", str(tmp_path), cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert payload["provider"] == "nope"
    assert payload["refreshed"] is False
    assert payload["models"] == []
    assert {"workspace", "model_metadata", "source", "discovery_mode"} <= set(payload)


def test_provider_models_refresh_without_discovery_endpoint_falls_back(tmp_path: Path) -> None:
    result = run_cli("provider", "models", "nope", "--refresh", "--workspace", str(tmp_path), cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == 11
    assert payload["refreshed"] is True
    assert payload["source"] == "fallback"
    assert payload["last_error"]
    assert "WARN provider.models.refresh" in result.stderr


def test_provider_inspect_reports_an_unconfigured_provider(tmp_path: Path) -> None:
    result = run_cli("provider", "inspect", "nope", "--workspace", str(tmp_path), cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == 11
    assert payload["provider"]["name"] == "nope"
    assert payload["provider"]["configured"] is False
    assert payload["validation"]["ok"] is False
    assert payload["validation"]["status"] == "unconfigured"
    assert payload["readiness"] is None
    assert {"endpoint", "models", "current_model", "current_model_metadata"} <= set(payload)


def test_provider_inspect_resolves_a_configured_model_without_leaking_the_key(tmp_path: Path) -> None:
    (tmp_path / ".voidcode.json").write_text(json.dumps({"model": "openai/gpt-4o"}), encoding="utf-8")

    result = run_cli(
        "provider",
        "inspect",
        "openai",
        "--workspace",
        str(tmp_path),
        cwd=tmp_path,
        env={"OPENAI_API_KEY": SENTINEL_KEY},
    )

    payload = json.loads(result.stdout)
    assert payload["provider"]["configured"] is True
    assert payload["provider"]["current"] is True
    assert payload["current_model"] == "gpt-4o"
    assert payload["readiness"]["ok"] is True
    assert payload["readiness"]["auth_present"] is True
    assert SENTINEL_KEY not in result.stdout
    assert SENTINEL_KEY not in result.stderr
