from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.config import (
    APPROVAL_MODE_ENV_VAR,
    RUNTIME_CONFIG_FILE_NAME,
    RuntimeHooksConfig,
    load_runtime_config,
    runtime_config_path,
)


def test_runtime_config_defaults_to_ask_without_file_or_env(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path, env={})

    assert config.approval_mode == "ask"
    assert config.model is None
    assert config.hooks is None


def test_runtime_config_uses_environment_when_repo_file_missing(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path, env={APPROVAL_MODE_ENV_VAR: "deny"})

    assert config.approval_mode == "deny"


def test_runtime_config_prefers_repo_file_over_environment(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {"approval_mode": "allow", "model": "opencode/gpt-5.4", "hooks": {"enabled": True}}
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={APPROVAL_MODE_ENV_VAR: "deny"})

    assert config.approval_mode == "allow"
    assert config.model == "opencode/gpt-5.4"
    assert config.hooks == RuntimeHooksConfig(enabled=True)


def test_runtime_config_prefers_explicit_override_over_repo_file_and_environment(
    tmp_path: Path,
) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"approval_mode": "deny"}),
        encoding="utf-8",
    )

    config = load_runtime_config(
        tmp_path,
        approval_mode="allow",
        env={APPROVAL_MODE_ENV_VAR: "ask"},
    )

    assert config.approval_mode == "allow"


def test_runtime_config_rejects_invalid_environment_approval_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=APPROVAL_MODE_ENV_VAR):
        _ = load_runtime_config(tmp_path, env={APPROVAL_MODE_ENV_VAR: "maybe"})


def test_runtime_config_rejects_invalid_repo_local_payload(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_rejects_invalid_repo_local_approval_mode(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"approval_mode": "maybe"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="approval_mode"):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_uses_repo_local_filename_inside_workspace(tmp_path: Path) -> None:
    config_file = tmp_path / RUNTIME_CONFIG_FILE_NAME

    assert runtime_config_path(tmp_path) == config_file
