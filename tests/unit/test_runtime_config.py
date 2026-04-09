from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.config import (
    APPROVAL_MODE_ENV_VAR,
    MODEL_ENV_VAR,
    RUNTIME_CONFIG_FILE_NAME,
    RuntimeAcpConfig,
    RuntimeHooksConfig,
    RuntimeLspConfig,
    RuntimeSkillsConfig,
    RuntimeToolsBuiltinConfig,
    RuntimeToolsConfig,
    load_runtime_config,
    runtime_config_path,
)


def test_runtime_config_defaults_to_ask_without_file_or_env(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path, env={})

    assert config.approval_mode == "ask"
    assert config.model is None
    assert config.execution_engine == "deterministic"
    assert config.hooks is None


def test_runtime_config_uses_environment_when_repo_file_missing(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path, env={APPROVAL_MODE_ENV_VAR: "deny"})

    assert config.approval_mode == "deny"


def test_runtime_config_uses_model_environment_when_repo_file_missing(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path, env={MODEL_ENV_VAR: "opencode/gpt-5.4"})

    assert config.model == "opencode/gpt-5.4"


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


def test_runtime_config_prefers_explicit_model_override_over_repo_file_and_environment(
    tmp_path: Path,
) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"model": "repo/model"}),
        encoding="utf-8",
    )

    config = load_runtime_config(
        tmp_path,
        model="explicit/model",
        env={MODEL_ENV_VAR: "env/model"},
    )

    assert config.model == "explicit/model"


def test_runtime_config_prefers_repo_file_model_over_environment(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"model": "repo/model"}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={MODEL_ENV_VAR: "env/model"})

    assert config.model == "repo/model"


def test_runtime_config_parses_extension_domains(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "execution_engine": "deterministic",
                "tools": {
                    "builtin": {"enabled": True},
                    "paths": [".voidcode/tools", "vendor/tools"],
                },
                "skills": {
                    "enabled": True,
                    "paths": [".voidcode/skills"],
                },
                "lsp": {
                    "enabled": False,
                    "servers": {"pyright": {"command": ["pyright-langserver", "--stdio"]}},
                },
                "acp": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.execution_engine == "deterministic"
    assert config.tools == RuntimeToolsConfig(
        builtin=RuntimeToolsBuiltinConfig(enabled=True),
        paths=(".voidcode/tools", "vendor/tools"),
    )
    assert config.skills == RuntimeSkillsConfig(
        enabled=True,
        paths=(".voidcode/skills",),
    )
    assert config.lsp == RuntimeLspConfig(
        enabled=False,
        servers={"pyright": {"command": ["pyright-langserver", "--stdio"]}},
    )
    assert config.acp == RuntimeAcpConfig(enabled=False)


def test_runtime_config_accepts_single_agent_execution_engine(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"execution_engine": "single_agent", "model": "opencode/gpt-5.4"}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.execution_engine == "single_agent"
    assert config.model == "opencode/gpt-5.4"


def test_runtime_config_parses_minimal_hook_commands(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "hooks": {
                    "enabled": True,
                    "pre_tool": [["python", "scripts/pre.py"]],
                    "post_tool": [["python", "scripts/post.py"]],
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks == RuntimeHooksConfig(
        enabled=True,
        pre_tool=(("python", "scripts/pre.py"),),
        post_tool=(("python", "scripts/post.py"),),
    )


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


def test_runtime_config_rejects_empty_model_environment(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=MODEL_ENV_VAR):
        _ = load_runtime_config(tmp_path, env={MODEL_ENV_VAR: ""})


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


def test_runtime_config_rejects_invalid_repo_local_execution_engine(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"execution_engine": "agent"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="execution_engine"):
        _ = load_runtime_config(tmp_path, env={})


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param({"tools": []}, "runtime config field 'tools'", id="tools-shape"),
        pytest.param(
            {"tools": {"builtin": {"enabled": "yes"}}},
            "runtime config field 'tools.builtin.enabled'",
            id="tools-builtin-enabled-type",
        ),
        pytest.param(
            {"tools": {"paths": [".voidcode/tools", 3]}},
            "runtime config field 'tools.paths\\[1\\]'",
            id="tools-path-item-type",
        ),
        pytest.param({"skills": []}, "runtime config field 'skills'", id="skills-shape"),
        pytest.param(
            {"skills": {"enabled": "yes"}},
            "runtime config field 'skills.enabled'",
            id="skills-enabled-type",
        ),
        pytest.param(
            {"skills": {"paths": [False]}},
            "runtime config field 'skills.paths\\[0\\]'",
            id="skills-path-item-type",
        ),
        pytest.param({"lsp": []}, "runtime config field 'lsp'", id="lsp-shape"),
        pytest.param(
            {"lsp": {"enabled": "no"}},
            "runtime config field 'lsp.enabled'",
            id="lsp-enabled-type",
        ),
        pytest.param(
            {"lsp": {"servers": []}},
            "runtime config field 'lsp.servers'",
            id="lsp-servers-shape",
        ),
        pytest.param({"acp": []}, "runtime config field 'acp'", id="acp-shape"),
        pytest.param(
            {"acp": {"enabled": "no"}},
            "runtime config field 'acp.enabled'",
            id="acp-enabled-type",
        ),
        pytest.param(
            {"hooks": {"pre_tool": "python scripts/pre.py"}},
            "runtime config field 'hooks.pre_tool'",
            id="hooks-pre-tool-shape",
        ),
        pytest.param(
            {"hooks": {"post_tool": [["python"], [False]]}},
            "runtime config field 'hooks.post_tool\\[1\\]\\[0\\]'",
            id="hooks-post-tool-command-item-shape",
        ),
        pytest.param(
            cast(dict[str, object], {"hooks": {"pre_tool": [[]]}}),
            "runtime config field 'hooks.pre_tool\\[0\\]'.*at least one string",
            id="hooks-pre-tool-empty-command",
        ),
        pytest.param(
            cast(dict[str, object], {"hooks": {"post_tool": [["echo", "hello"], []]}}),
            "runtime config field 'hooks.post_tool\\[1\\]'.*at least one string",
            id="hooks-post-tool-empty-command",
        ),
    ],
)
def test_runtime_config_rejects_invalid_extension_domain_shapes(
    tmp_path: Path,
    payload: dict[str, object],
    match: str,
) -> None:
    runtime_config_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        _ = load_runtime_config(tmp_path, env={})


def test_runtime_config_uses_repo_local_filename_inside_workspace(tmp_path: Path) -> None:
    config_file = tmp_path / RUNTIME_CONFIG_FILE_NAME

    assert runtime_config_path(tmp_path) == config_file
