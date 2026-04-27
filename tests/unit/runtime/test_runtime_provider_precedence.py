from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.provider.config import SimplifiedProviderConfig
from voidcode.runtime.config import load_runtime_config


def test_runtime_config_providers_prefer_environment_over_global_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config-home"
    user_config_dir = config_home / "voidcode"
    user_config_dir.mkdir(parents=True)
    (user_config_dir / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "opencode-go": {
                        "api_key": "global-key",
                        "base_url": "https://global.example/v1",
                        "model_map": {"fast": "kimi-k2.6"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = load_runtime_config(
        workspace,
        env={"XDG_CONFIG_HOME": str(config_home), "OPENCODE_API_KEY": "env-key"},
    )

    assert config.providers is not None
    assert config.providers.opencode_go == SimplifiedProviderConfig(
        api_key="env-key",
        base_url="https://global.example/v1",
        model_map={"fast": "kimi-k2.6"},
    )
