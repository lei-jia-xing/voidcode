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
    result = run_cli("provider", "models", "mistral", "--workspace", str(tmp_path), cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert payload["provider"] == "mistral"
    assert payload["refreshed"] is False
    assert payload["models"] == []
    assert {"workspace", "model_metadata", "source", "discovery_mode"} <= set(payload)


def test_provider_models_rejects_an_undeclared_provider(tmp_path: Path) -> None:
    result = run_cli("provider", "models", "nope", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == 12
    assert result.stdout == ""
    assert "unknown provider id 'nope'" in result.stderr
    assert "known provider ids are" in result.stderr
    assert "providers.custom.nope" in result.stderr


def test_provider_models_refresh_without_discovery_endpoint_falls_back(tmp_path: Path) -> None:
    result = run_cli("provider", "models", "minimax", "--refresh", "--workspace", str(tmp_path), cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == 11
    assert payload["refreshed"] is True
    assert payload["source"] == "fallback"
    assert payload["last_error"]
    assert "WARN provider.models.refresh" in result.stderr


def test_provider_inspect_reports_an_unconfigured_provider(tmp_path: Path) -> None:
    result = run_cli("provider", "inspect", "mistral", "--workspace", str(tmp_path), cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == 11
    assert payload["provider"]["name"] == "mistral"
    assert payload["provider"]["label"] == "Mistral"
    assert payload["provider"]["configured"] is False
    assert payload["validation"]["ok"] is False
    assert payload["validation"]["status"] == "unconfigured"
    assert payload["readiness"] is None
    assert {"endpoint", "models", "current_model", "current_model_metadata"} <= set(payload)


def test_provider_inspect_rejects_an_undeclared_provider(tmp_path: Path) -> None:
    result = run_cli("provider", "inspect", "nope", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == 12
    assert result.stdout == ""
    assert "unknown provider id 'nope'" in result.stderr
    assert "providers.custom.nope" in result.stderr


def test_provider_inspect_canonicalises_a_spelling_variant(tmp_path: Path) -> None:
    # `MiniMax` is the label of the `minimax` provider: either spelling names the
    # same id, and the payload reports one `name` plus one `label`.
    canonical = run_cli("provider", "inspect", "minimax", "--workspace", str(tmp_path), cwd=tmp_path)
    variant = run_cli("provider", "inspect", "MiniMax", "--workspace", str(tmp_path), cwd=tmp_path)

    canonical_payload = json.loads(canonical.stdout)
    variant_payload = json.loads(variant.stdout)
    assert (variant_payload["provider"]["name"], variant_payload["provider"]["label"]) == ("minimax", "MiniMax")
    assert variant_payload["endpoint"] == canonical_payload["endpoint"]
    assert variant_payload["validation"] == canonical_payload["validation"]


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
