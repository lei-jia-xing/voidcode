from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.config import (
    APPROVAL_MODE_ENV_VAR,
    MODEL_ENV_VAR,
    RUNTIME_CONFIG_FILE_NAME,
    RuntimeAcpConfig,
    RuntimeFormatterPresetConfig,
    RuntimeHooksConfig,
    RuntimeLspConfig,
    RuntimeLspServerConfig,
    RuntimeProviderFallbackConfig,
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
    assert config.max_steps == 4
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
                "max_steps": 6,
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
                "provider_fallback": {
                    "preferred_model": "opencode/gpt-5.4",
                    "fallback_models": ["opencode/gpt-5.3", "custom/demo"],
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.execution_engine == "deterministic"
    assert config.max_steps == 6
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
        servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
    )
    assert config.acp == RuntimeAcpConfig(enabled=False)
    assert config.provider_fallback == RuntimeProviderFallbackConfig(
        preferred_model="opencode/gpt-5.4",
        fallback_models=("opencode/gpt-5.3", "custom/demo"),
    )


def test_runtime_config_accepts_builtin_lsp_preset_without_explicit_command(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"lsp": {"enabled": True, "servers": {"pyright": {}}}}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.lsp == RuntimeLspConfig(
        enabled=True,
        servers={"pyright": RuntimeLspServerConfig()},
    )


def test_runtime_config_accepts_explicit_lsp_preset_override(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "lsp": {
                    "enabled": True,
                    "servers": {
                        "python": {
                            "preset": "pyright",
                            "extensions": [".pyw"],
                            "root_markers": ["requirements-dev.txt"],
                            "settings": {"python": {"analysis": {"typeCheckingMode": "strict"}}},
                            "init_options": {"diagnostics": {"enable": True}},
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.lsp == RuntimeLspConfig(
        enabled=True,
        servers={
            "python": RuntimeLspServerConfig(
                preset="pyright",
                extensions=(".pyw",),
                root_markers=("requirements-dev.txt",),
                settings={"python": {"analysis": {"typeCheckingMode": "strict"}}},
                init_options={"diagnostics": {"enable": True}},
            )
        },
    )


def test_runtime_config_accepts_single_agent_execution_engine(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"execution_engine": "single_agent", "model": "opencode/gpt-5.4"}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.execution_engine == "single_agent"
    assert config.model == "opencode/gpt-5.4"


def test_runtime_config_parses_repo_local_max_steps(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"max_steps": 7}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.max_steps == 7


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


def test_runtime_config_parses_formatter_preset_hooks(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "hooks": {
                    "enabled": True,
                    "formatter_presets": {
                        "python": {"command": ["ruff", "format"]},
                        "typescript": {"command": ["prettier", "--write"]},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks == RuntimeHooksConfig(
        enabled=True,
        formatter_presets={
            "python": RuntimeFormatterPresetConfig(command=("ruff", "format")),
            "javascript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "json": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "markdown": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "yaml": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "rust": RuntimeFormatterPresetConfig(command=("rustfmt",)),
            "go": RuntimeFormatterPresetConfig(command=("gofmt",)),
            "typescript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        },
    )


def test_runtime_hooks_config_defaults_formatter_presets_to_common_language_builtins() -> None:
    assert RuntimeHooksConfig().formatter_presets == {
        "python": RuntimeFormatterPresetConfig(command=("ruff", "format")),
        "typescript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "javascript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "json": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "markdown": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "yaml": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "rust": RuntimeFormatterPresetConfig(command=("rustfmt",)),
        "go": RuntimeFormatterPresetConfig(command=("gofmt",)),
    }


def test_runtime_config_keeps_builtin_formatter_presets_when_hooks_formatter_presets_missing(
    tmp_path: Path,
) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "hooks": {
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks == RuntimeHooksConfig(enabled=True)


def test_runtime_config_overrides_builtin_formatter_preset_with_user_value(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "hooks": {
                    "enabled": True,
                    "formatter_presets": {
                        "python": {"command": ["uvx", "ruff", "format"]},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks == RuntimeHooksConfig(
        enabled=True,
        formatter_presets={
            "python": RuntimeFormatterPresetConfig(command=("uvx", "ruff", "format")),
            "typescript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "javascript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "json": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "markdown": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "yaml": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "rust": RuntimeFormatterPresetConfig(command=("rustfmt",)),
            "go": RuntimeFormatterPresetConfig(command=("gofmt",)),
        },
    )


def test_runtime_config_keeps_builtin_formatter_presets_when_adding_custom_user_preset(
    tmp_path: Path,
) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "hooks": {
                    "enabled": True,
                    "formatter_presets": {
                        "toml": {"command": ["taplo", "fmt"]},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks == RuntimeHooksConfig(
        enabled=True,
        formatter_presets={
            "python": RuntimeFormatterPresetConfig(command=("ruff", "format")),
            "typescript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "javascript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "json": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "markdown": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "yaml": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
            "rust": RuntimeFormatterPresetConfig(command=("rustfmt",)),
            "go": RuntimeFormatterPresetConfig(command=("gofmt",)),
            "toml": RuntimeFormatterPresetConfig(command=("taplo", "fmt")),
        },
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
        pytest.param({"max_steps": 0}, "runtime config field 'max_steps'", id="max-steps-zero"),
        pytest.param(
            {"max_steps": -1}, "runtime config field 'max_steps'", id="max-steps-negative"
        ),
        pytest.param(
            {"max_steps": "four"}, "runtime config field 'max_steps'", id="max-steps-type"
        ),
    ],
)
def test_runtime_config_rejects_invalid_max_steps(
    tmp_path: Path, payload: dict[str, object], match: str
) -> None:
    runtime_config_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=match):
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
        pytest.param(
            {"lsp": {"servers": {"pyright": []}}},
            "runtime config field 'lsp.servers.pyright'",
            id="lsp-server-shape",
        ),
        pytest.param(
            {"lsp": {"servers": {"custom": {"command": []}}}},
            "runtime config field 'lsp.servers.custom.command'.*at least one string",
            id="lsp-server-command-empty",
        ),
        pytest.param(
            {"lsp": {"servers": {"pyright": {"command": [False]}}}},
            "runtime config field 'lsp.servers.pyright.command\\[0\\]'",
            id="lsp-server-command-item-type",
        ),
        pytest.param(
            {"lsp": {"servers": {"pyright": {"command": ["pyright"], "languages": [1]}}}},
            "runtime config field 'lsp.servers.pyright.languages\\[0\\]'",
            id="lsp-server-language-item-type",
        ),
        pytest.param(
            {"lsp": {"servers": {"python": {"preset": 1}}}},
            "runtime config field 'lsp.servers.python.preset'",
            id="lsp-server-preset-type",
        ),
        pytest.param(
            {"lsp": {"servers": {"python": {"preset": "not-real"}}}},
            "runtime config field 'lsp.servers.python.preset' references unknown preset",
            id="lsp-server-preset-unknown",
        ),
        pytest.param(
            {"lsp": {"servers": {"pyright": {"extensions": [1]}}}},
            "runtime config field 'lsp.servers.pyright.extensions\\[0\\]'",
            id="lsp-server-extension-item-type",
        ),
        pytest.param(
            {"lsp": {"servers": {"pyright": {"root_markers": [1]}}}},
            "runtime config field 'lsp.servers.pyright.root_markers\\[0\\]'",
            id="lsp-server-root-marker-item-type",
        ),
        pytest.param(
            {"lsp": {"servers": {"pyright": {"settings": []}}}},
            "runtime config field 'lsp.servers.pyright.settings'",
            id="lsp-server-settings-shape",
        ),
        pytest.param(
            {"lsp": {"servers": {"pyright": {"init_options": []}}}},
            "runtime config field 'lsp.servers.pyright.init_options'",
            id="lsp-server-init-options-shape",
        ),
        pytest.param(
            {"provider_fallback": []},
            "runtime config field 'provider_fallback'",
            id="provider-fallback-shape",
        ),
        pytest.param(
            {"provider_fallback": {"preferred_model": 1}},
            "runtime config field 'provider_fallback.preferred_model'",
            id="provider-fallback-preferred-type",
        ),
        pytest.param(
            {"provider_fallback": {"preferred_model": "opencode/gpt-5.4", "fallback_models": [1]}},
            "runtime config field 'provider_fallback.fallback_models\\[0\\]'",
            id="provider-fallback-list-item-type",
        ),
        pytest.param(
            {
                "provider_fallback": {
                    "preferred_model": "opencode/gpt-5.4",
                    "fallback_models": ["opencode/gpt-5.4"],
                }
            },
            "provider fallback chain must not contain duplicate models",
            id="provider-fallback-duplicates",
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
        pytest.param(
            {"hooks": {"formatter_presets": []}},
            "runtime config field 'hooks.formatter_presets'",
            id="hooks-formatter-presets-shape",
        ),
        pytest.param(
            {"hooks": {"formatter_presets": {"python": []}}},
            "runtime config field 'hooks.formatter_presets.python'",
            id="hooks-formatter-preset-shape",
        ),
        pytest.param(
            {"hooks": {"formatter_presets": {"python": {"command": []}}}},
            "runtime config field 'hooks.formatter_presets.python.command'.*at least one string",
            id="hooks-formatter-preset-command-empty",
        ),
        pytest.param(
            {"hooks": {"formatter_presets": {"python": {"command": [False]}}}},
            "runtime config field 'hooks.formatter_presets.python.command\\[0\\]'",
            id="hooks-formatter-preset-command-item-type",
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
