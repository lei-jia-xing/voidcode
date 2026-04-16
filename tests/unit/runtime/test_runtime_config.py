from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from voidcode.provider.config import (
    AnthropicProviderConfig,
    CopilotProviderAuthConfig,
    CopilotProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
    OpenAIProviderConfig,
)
from voidcode.runtime import config as runtime_config
from voidcode.runtime.config import (
    APPROVAL_MODE_ENV_VAR,
    EXECUTION_ENGINE_ENV_VAR,
    MAX_STEPS_ENV_VAR,
    MODEL_ENV_VAR,
    RUNTIME_CONFIG_FILE_NAME,
    RuntimeAcpConfig,
    RuntimeFormatterPresetConfig,
    RuntimeHooksConfig,
    RuntimeLspConfig,
    RuntimeLspServerConfig,
    RuntimePlanConfig,
    RuntimeProviderFallbackConfig,
    RuntimeProvidersConfig,
    RuntimeSkillsConfig,
    RuntimeToolsBuiltinConfig,
    RuntimeToolsConfig,
    RuntimeTuiConfig,
    RuntimeTuiPreferences,
    RuntimeTuiReadingPreferences,
    RuntimeTuiThemePreferences,
    effective_runtime_tui_preferences,
    load_runtime_config,
    runtime_config_path,
    save_global_tui_preferences,
    save_workspace_tui_preferences,
    user_runtime_config_path,
)

_parse_tui_config = runtime_config.__dict__["_parse_tui_config"]
_parse_tools_config = runtime_config.__dict__["_parse_tools_config"]
_parse_skills_config = runtime_config.__dict__["_parse_skills_config"]
_parse_acp_config = runtime_config.__dict__["_parse_acp_config"]

PRETTIER_ROOT_MARKERS = (
    "package.json",
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
)

PRETTIER_FALLBACK_COMMANDS = (
    ("bunx", "prettier", "--write"),
    ("pnpm", "exec", "prettier", "--write"),
    ("npx", "prettier", "--write"),
)


def _prettier_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("prettier", "--write"),
        extensions=extensions,
        root_markers=PRETTIER_ROOT_MARKERS,
        fallback_commands=PRETTIER_FALLBACK_COMMANDS,
        cwd_policy="nearest_root",
    )


def _shfmt_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("shfmt", "-w"),
        extensions=extensions,
        root_markers=(".editorconfig", ".shfmt.conf", ".shfmt"),
        cwd_policy="nearest_root",
    )


def _dockerfmt_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("dockerfmt", "--write"),
        extensions=extensions,
        root_markers=(".dockerfmt.toml", ".dockerfmt.hcl", "Dockerfile"),
        cwd_policy="nearest_root",
    )


def _clang_format_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("clang-format", "-i"),
        extensions=extensions,
        root_markers=(".clang-format", "_clang-format", "compile_commands.json", "CMakeLists.txt"),
        cwd_policy="nearest_root",
    )


def _sql_formatter_preset() -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("sql-formatter", "--fix"),
        extensions=(".sql",),
        root_markers=(".sql-formatter.json", ".sql-formatter.jsonc", "package.json"),
        fallback_commands=(
            ("bunx", "sql-formatter", "--fix"),
            ("pnpm", "exec", "sql-formatter", "--fix"),
            ("npx", "sql-formatter", "--fix"),
        ),
        cwd_policy="nearest_root",
    )


DEFAULT_FORMATTER_PRESETS = {
    "python": RuntimeFormatterPresetConfig(
        command=("ruff", "format"),
        extensions=(".py", ".pyi"),
        root_markers=("pyproject.toml", "ruff.toml", ".ruff.toml"),
        fallback_commands=(("uvx", "ruff", "format"), ("python", "-m", "ruff", "format")),
        cwd_policy="nearest_root",
    ),
    "typescript": _prettier_preset(".ts", ".tsx", ".mts", ".cts"),
    "javascript": _prettier_preset(".js", ".jsx", ".mjs", ".cjs"),
    "json": _prettier_preset(".json", ".jsonc"),
    "markdown": _prettier_preset(".md", ".mdx"),
    "yaml": _prettier_preset(".yaml", ".yml"),
    "html": _prettier_preset(".html", ".htm"),
    "css": _prettier_preset(".css"),
    "scss": _prettier_preset(".scss"),
    "less": _prettier_preset(".less"),
    "vue": _prettier_preset(".vue"),
    "svelte": _prettier_preset(".svelte"),
    "astro": _prettier_preset(".astro"),
    "graphql": _prettier_preset(".graphql", ".gql"),
    "handlebars": _prettier_preset(".hbs", ".handlebars"),
    "toml": RuntimeFormatterPresetConfig(
        command=("taplo", "fmt"),
        extensions=(".toml",),
        root_markers=("taplo.toml", ".taplo.toml", "pyproject.toml", "Cargo.toml"),
        cwd_policy="nearest_root",
    ),
    "shell": _shfmt_preset(".sh", ".bash", ".zsh"),
    "dockerfile": _dockerfmt_preset("Dockerfile"),
    "nix": RuntimeFormatterPresetConfig(
        command=("nixfmt",),
        extensions=(".nix",),
        root_markers=("flake.nix", "shell.nix", "default.nix"),
        cwd_policy="nearest_root",
    ),
    "sql": _sql_formatter_preset(),
    "rust": RuntimeFormatterPresetConfig(
        command=("rustfmt",),
        extensions=(".rs",),
        root_markers=("Cargo.toml", "rustfmt.toml", ".rustfmt.toml"),
        cwd_policy="nearest_root",
    ),
    "go": RuntimeFormatterPresetConfig(
        command=("gofmt", "-w"),
        extensions=(".go",),
        root_markers=("go.mod",),
        cwd_policy="nearest_root",
    ),
    "c": _clang_format_preset(".c", ".h"),
    "cpp": _clang_format_preset(".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"),
    "java": RuntimeFormatterPresetConfig(
        command=("google-java-format", "--replace"),
        extensions=(".java",),
        root_markers=(".google-java-format", "pom.xml", "build.gradle", "build.gradle.kts"),
        cwd_policy="nearest_root",
    ),
    "kotlin": RuntimeFormatterPresetConfig(
        command=("ktlint", "-F"),
        extensions=(".kt", ".kts"),
        root_markers=("ktlint.yml", ".editorconfig", "build.gradle.kts"),
        cwd_policy="nearest_root",
    ),
    "xml": _prettier_preset(".xml"),
}


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


def test_runtime_config_uses_execution_engine_environment_when_repo_file_missing(
    tmp_path: Path,
) -> None:
    config = load_runtime_config(tmp_path, env={EXECUTION_ENGINE_ENV_VAR: "single_agent"})

    assert config.execution_engine == "single_agent"


def test_runtime_config_uses_max_steps_environment_when_repo_file_missing(tmp_path: Path) -> None:
    config = load_runtime_config(tmp_path, env={MAX_STEPS_ENV_VAR: "7"})

    assert config.max_steps == 7


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


def test_runtime_config_parses_async_lifecycle_hook_surfaces(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "hooks": {
                    "enabled": True,
                    "pre_tool": [["python", "scripts/pre_tool.py"]],
                    "post_tool": [["python", "scripts/post_tool.py"]],
                    "on_session_start": [["python", "scripts/session_start.py"]],
                    "on_session_end": [["python", "scripts/session_end.py"]],
                    "on_session_idle": [["python", "scripts/session_idle.py"]],
                    "on_background_task_completed": [["python", "scripts/task_completed.py"]],
                    "on_background_task_failed": [["python", "scripts/task_failed.py"]],
                    "on_background_task_cancelled": [["python", "scripts/task_cancelled.py"]],
                    "on_delegated_result_available": [["python", "scripts/delegated_result.py"]],
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.hooks == RuntimeHooksConfig(
        enabled=True,
        pre_tool=(("python", "scripts/pre_tool.py"),),
        post_tool=(("python", "scripts/post_tool.py"),),
        on_session_start=(("python", "scripts/session_start.py"),),
        on_session_end=(("python", "scripts/session_end.py"),),
        on_session_idle=(("python", "scripts/session_idle.py"),),
        on_background_task_completed=(("python", "scripts/task_completed.py"),),
        on_background_task_failed=(("python", "scripts/task_failed.py"),),
        on_background_task_cancelled=(("python", "scripts/task_cancelled.py"),),
        on_delegated_result_available=(("python", "scripts/delegated_result.py"),),
    )


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


def test_runtime_config_prefers_repo_file_execution_engine_over_environment(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"execution_engine": "deterministic"}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={EXECUTION_ENGINE_ENV_VAR: "single_agent"})

    assert config.execution_engine == "deterministic"


def test_runtime_config_prefers_repo_file_max_steps_over_environment(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"max_steps": 4}),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={MAX_STEPS_ENV_VAR: "7"})

    assert config.max_steps == 4


def test_runtime_config_prefers_explicit_execution_engine_over_repo_file_and_environment(
    tmp_path: Path,
) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"execution_engine": "deterministic"}),
        encoding="utf-8",
    )

    config = load_runtime_config(
        tmp_path,
        execution_engine="single_agent",
        env={EXECUTION_ENGINE_ENV_VAR: "deterministic"},
    )

    assert config.execution_engine == "single_agent"


def test_runtime_config_prefers_explicit_max_steps_over_repo_file_and_environment(
    tmp_path: Path,
) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps({"max_steps": 4}),
        encoding="utf-8",
    )

    config = load_runtime_config(
        tmp_path,
        max_steps=9,
        env={MAX_STEPS_ENV_VAR: "7"},
    )

    assert config.max_steps == 9


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
                "providers": {
                    "openai": {
                        "api_key": "openai-inline-key",
                        "base_url": "https://api.openai.test/v1",
                        "organization": "org_123",
                        "project": "proj_123",
                        "timeout_seconds": 30,
                    },
                    "anthropic": {
                        "api_key": "anthropic-inline-key",
                        "base_url": "https://api.anthropic.test",
                        "version": "2023-06-01",
                        "beta_headers": ["prompt-caching-2024-07-31"],
                        "timeout_seconds": 45,
                    },
                    "google": {
                        "auth": {
                            "method": "api_key",
                            "api_key": "google-inline-key",
                        },
                        "base_url": "https://generativelanguage.googleapis.com",
                        "project": "project-123",
                        "region": "us-central1",
                        "timeout_seconds": 20,
                    },
                    "copilot": {
                        "auth": {
                            "method": "token",
                            "token": "copilot-inline-token",
                        },
                        "base_url": "https://api.githubcopilot.test",
                        "timeout_seconds": 15,
                    },
                    "litellm": {
                        "api_key": "litellm-inline-key",
                        "base_url": "http://127.0.0.1:4000",
                        "auth_scheme": "token",
                        "auth_header": "X-LiteLLM-Key",
                        "timeout_seconds": 10,
                        "model_map": {"gpt-4o": "openrouter/openai/gpt-4o"},
                    },
                    "custom": {
                        "llama-local": {
                            "base_url": "http://localhost:11434/v1",
                            "auth_scheme": "none",
                            "model_map": {"coder": "ollama/qwen2.5-coder:latest"},
                        }
                    },
                },
                "plan": {
                    "provider": "custom",
                    "module": "./.voidcode/plan_extension.py",
                    "factory": "build",
                    "options": {"mode": "strict"},
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
    assert config.providers == RuntimeProvidersConfig(
        openai=OpenAIProviderConfig(
            api_key="openai-inline-key",
            base_url="https://api.openai.test/v1",
            organization="org_123",
            project="proj_123",
            timeout_seconds=30.0,
        ),
        anthropic=AnthropicProviderConfig(
            api_key="anthropic-inline-key",
            base_url="https://api.anthropic.test",
            version="2023-06-01",
            beta_headers=("prompt-caching-2024-07-31",),
            timeout_seconds=45.0,
        ),
        google=GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(method="api_key", api_key="google-inline-key"),
            base_url="https://generativelanguage.googleapis.com",
            project="project-123",
            region="us-central1",
            timeout_seconds=20.0,
        ),
        copilot=CopilotProviderConfig(
            auth=CopilotProviderAuthConfig(method="token", token="copilot-inline-token"),
            base_url="https://api.githubcopilot.test",
            timeout_seconds=15.0,
        ),
        litellm=LiteLLMProviderConfig(
            api_key="litellm-inline-key",
            base_url="http://127.0.0.1:4000",
            auth_scheme="token",
            auth_header="X-LiteLLM-Key",
            timeout_seconds=10.0,
            model_map={"gpt-4o": "openrouter/openai/gpt-4o"},
        ),
        custom={
            "llama-local": LiteLLMProviderConfig(
                base_url="http://localhost:11434/v1",
                auth_scheme="none",
                model_map={"coder": "ollama/qwen2.5-coder:latest"},
            )
        },
    )
    assert config.plan == RuntimePlanConfig(
        provider="custom",
        module="./.voidcode/plan_extension.py",
        factory="build",
        options={"mode": "strict"},
    )


def test_runtime_config_providers_use_environment_secrets_when_omitted(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {},
                    "anthropic": {},
                    "google": {"auth": {"method": "api_key"}},
                    "copilot": {"auth": {"method": "token", "token_env_var": "COPILOT_TOKEN"}},
                    "litellm": {},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(
        tmp_path,
        env={
            "OPENAI_API_KEY": "openai-env-key",
            "ANTHROPIC_API_KEY": "anthropic-env-key",
            "GOOGLE_API_KEY": "google-env-key",
            "LITELLM_API_KEY": "litellm-env-key",
            "LITELLM_BASE_URL": "http://localhost:4000",
        },
    )

    assert config.providers == RuntimeProvidersConfig(
        openai=OpenAIProviderConfig(api_key="openai-env-key"),
        anthropic=AnthropicProviderConfig(api_key="anthropic-env-key"),
        google=GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(method="api_key", api_key="google-env-key")
        ),
        copilot=CopilotProviderConfig(
            auth=CopilotProviderAuthConfig(method="token", token_env_var="COPILOT_TOKEN")
        ),
        litellm=LiteLLMProviderConfig(
            api_key="litellm-env-key",
            base_url="http://localhost:4000",
            auth_scheme="bearer",
        ),
    )


def test_runtime_config_providers_prefer_repo_config_over_environment(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {"api_key": "openai-repo-key"},
                    "anthropic": {"api_key": "anthropic-repo-key"},
                    "google": {
                        "auth": {"method": "api_key", "api_key": "google-repo-key"},
                    },
                    "copilot": {
                        "auth": {"method": "token", "token": "copilot-repo-token"},
                    },
                    "litellm": {"api_key": "litellm-repo-key"},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(
        tmp_path,
        env={
            "OPENAI_API_KEY": "openai-env-key",
            "ANTHROPIC_API_KEY": "anthropic-env-key",
            "GOOGLE_API_KEY": "google-env-key",
            "GITHUB_COPILOT_TOKEN": "copilot-env-token",
            "LITELLM_API_KEY": "litellm-env-key",
        },
    )

    assert config.providers == RuntimeProvidersConfig(
        openai=OpenAIProviderConfig(api_key="openai-repo-key"),
        anthropic=AnthropicProviderConfig(api_key="anthropic-repo-key"),
        google=GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(method="api_key", api_key="google-repo-key")
        ),
        copilot=CopilotProviderConfig(
            auth=CopilotProviderAuthConfig(method="token", token="copilot-repo-token")
        ),
        litellm=LiteLLMProviderConfig(
            api_key="litellm-repo-key",
            auth_scheme="bearer",
        ),
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
        formatter_presets=DEFAULT_FORMATTER_PRESETS,
    )


def test_runtime_hooks_config_defaults_formatter_presets_to_common_language_builtins() -> None:
    assert RuntimeHooksConfig().formatter_presets == DEFAULT_FORMATTER_PRESETS


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
            **DEFAULT_FORMATTER_PRESETS,
            "python": RuntimeFormatterPresetConfig(
                command=("uvx", "ruff", "format"),
                extensions=(".py", ".pyi"),
                root_markers=("pyproject.toml", "ruff.toml", ".ruff.toml"),
                fallback_commands=(("uvx", "ruff", "format"), ("python", "-m", "ruff", "format")),
                cwd_policy="nearest_root",
            ),
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
                        "php": {
                            "command": ["php-cs-fixer", "fix"],
                            "extensions": [".php"],
                            "root_markers": ["composer.json", ".php-cs-fixer.php"],
                        },
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
            **DEFAULT_FORMATTER_PRESETS,
            "php": RuntimeFormatterPresetConfig(
                command=("php-cs-fixer", "fix"),
                extensions=(".php",),
                root_markers=("composer.json", ".php-cs-fixer.php"),
            ),
        },
    )


def test_runtime_config_merges_partial_builtin_formatter_override(tmp_path: Path) -> None:
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "hooks": {
                    "enabled": True,
                    "formatter_presets": {
                        "typescript": {
                            "extensions": [".ts", ".tsx", ".vue"],
                            "cwd_policy": "workspace",
                        },
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
            **DEFAULT_FORMATTER_PRESETS,
            "typescript": RuntimeFormatterPresetConfig(
                command=("prettier", "--write"),
                extensions=(".ts", ".tsx", ".vue"),
                root_markers=PRETTIER_ROOT_MARKERS,
                fallback_commands=(
                    ("bunx", "prettier", "--write"),
                    ("pnpm", "exec", "prettier", "--write"),
                    ("npx", "prettier", "--write"),
                ),
                cwd_policy="workspace",
            ),
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


def test_runtime_config_explicit_approval_mode_does_not_affect_environment_execution_engine(
    tmp_path: Path,
) -> None:
    config = load_runtime_config(
        tmp_path,
        approval_mode="allow",
        env={EXECUTION_ENGINE_ENV_VAR: "single_agent"},
    )

    assert config.approval_mode == "allow"
    assert config.execution_engine == "single_agent"


def test_runtime_config_rejects_invalid_environment_approval_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=APPROVAL_MODE_ENV_VAR):
        _ = load_runtime_config(tmp_path, env={APPROVAL_MODE_ENV_VAR: "maybe"})


def test_runtime_config_rejects_invalid_environment_execution_engine(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=EXECUTION_ENGINE_ENV_VAR):
        _ = load_runtime_config(tmp_path, env={EXECUTION_ENGINE_ENV_VAR: "agent"})


@pytest.mark.parametrize("raw_value", ["0", "-1", "four"])
def test_runtime_config_rejects_invalid_environment_max_steps(
    tmp_path: Path, raw_value: str
) -> None:
    with pytest.raises(ValueError, match=MAX_STEPS_ENV_VAR):
        _ = load_runtime_config(tmp_path, env={MAX_STEPS_ENV_VAR: raw_value})


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
        pytest.param(
            {"providers": []},
            "runtime config field 'providers'",
            id="providers-shape",
        ),
        pytest.param(
            {"providers": {"unknown": {}}},
            "runtime config field 'providers.unknown'",
            id="providers-unknown-provider",
        ),
        pytest.param(
            {"providers": {"openai": []}},
            "runtime config field 'providers.openai'",
            id="providers-openai-shape",
        ),
        pytest.param(
            {"providers": {"openai": {"api_key": 1}}},
            "runtime config field 'providers.openai.api_key'",
            id="providers-openai-api-key-type",
        ),
        pytest.param(
            {"providers": {"openai": {"timeout_seconds": 0}}},
            "runtime config field 'providers.openai.timeout_seconds'",
            id="providers-openai-timeout-invalid",
        ),
        pytest.param(
            {"providers": {"anthropic": {"beta_headers": [False]}}},
            "runtime config field 'providers.anthropic.beta_headers\\[0\\]'",
            id="providers-anthropic-beta-header-item-type",
        ),
        pytest.param(
            {"providers": {"google": {"auth": {"method": "invalid"}}}},
            "runtime config field 'providers.google.auth.method'",
            id="providers-google-auth-method-invalid",
        ),
        pytest.param(
            {"providers": {"google": {"auth": {"method": "api_key"}}}},
            "runtime config field 'providers.google.auth.api_key'",
            id="providers-google-api-key-missing",
        ),
        pytest.param(
            {
                "providers": {
                    "google": {"auth": {"method": "oauth", "api_key": "x", "access_token": "y"}}
                }
            },
            "runtime config field 'providers.google.auth.api_key'",
            id="providers-google-oauth-conflict",
        ),
        pytest.param(
            {
                "providers": {
                    "copilot": {"auth": {"method": "token", "token": "a", "token_env_var": "TOKEN"}}
                }
            },
            (
                "runtime config field 'providers.copilot.auth.token'.*"
                "runtime config field 'providers.copilot.auth.token_env_var'"
            ),
            id="providers-copilot-token-conflict",
        ),
        pytest.param(
            {
                "providers": {
                    "copilot": {"auth": {"method": "token", "token": "a", "refresh_token": "b"}}
                }
            },
            "runtime config field 'providers.copilot.auth.refresh_token'",
            id="providers-copilot-refresh-token-invalid-for-token-method",
        ),
        pytest.param(
            {
                "providers": {
                    "copilot": {
                        "auth": {
                            "method": "oauth",
                            "token_env_var": "TOKEN",
                            "refresh_leeway_seconds": 0,
                        }
                    }
                }
            },
            "runtime config field 'providers.copilot.auth.refresh_leeway_seconds'",
            id="providers-copilot-refresh-leeway-invalid",
        ),
        pytest.param(
            {"providers": {"litellm": {"auth_scheme": "oauth"}}},
            "runtime config field 'providers.litellm.auth_scheme'",
            id="providers-litellm-auth-scheme-invalid",
        ),
        pytest.param(
            {"providers": {"litellm": {"model_map": {"gpt-4o": 4}}}},
            "runtime config field 'providers.litellm.model_map.gpt-4o'",
            id="providers-litellm-model-map-value-invalid",
        ),
        pytest.param(
            {"plan": []},
            "runtime config field 'plan'",
            id="plan-shape",
        ),
        pytest.param(
            {"plan": {"provider": ""}},
            "runtime config field 'plan.provider'",
            id="plan-provider-empty",
        ),
        pytest.param(
            {"plan": {"module": ""}},
            "runtime config field 'plan.module'",
            id="plan-module-empty",
        ),
        pytest.param(
            {"plan": {"factory": ""}},
            "runtime config field 'plan.factory'",
            id="plan-factory-empty",
        ),
        pytest.param(
            {"plan": {"options": []}},
            "runtime config field 'plan.options'",
            id="plan-options-shape",
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
        pytest.param(
            {"hooks": {"formatter_presets": {"python": {"cwd_policy": "repo"}}}},
            "runtime config field 'hooks.formatter_presets.python.cwd_policy'",
            id="hooks-formatter-preset-cwd-policy",
        ),
        pytest.param(
            {"hooks": {"formatter_presets": {"python": {"extensions": [False]}}}},
            "runtime config field 'hooks.formatter_presets.python.extensions\\[0\\]'",
            id="hooks-formatter-preset-extension-item-type",
        ),
        pytest.param(
            {"hooks": {"formatter_presets": {"python": {"root_markers": [False]}}}},
            "runtime config field 'hooks.formatter_presets.python.root_markers\\[0\\]'",
            id="hooks-formatter-preset-root-marker-item-type",
        ),
        pytest.param(
            {"hooks": {"formatter_presets": {"python": {"fallback_commands": [["uvx"], [False]]}}}},
            "runtime config field 'hooks.formatter_presets.python.fallback_commands\\[1\\]\\[0\\]'",
            id="hooks-formatter-preset-fallback-item-type",
        ),
        pytest.param(
            {"hooks": {"formatter_presets": {"php": {"command": ["php-cs-fixer", "fix"]}}}},
            "runtime config field 'hooks.formatter_presets.php.extensions'",
            id="hooks-formatter-preset-custom-name-missing-extension",
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


def test_parse_tui_config_returns_defaults_when_fields_missing() -> None:
    assert _parse_tui_config({}) == RuntimeTuiConfig(leader_key=None, keymap=None)


def test_parse_tui_config_preserves_preferences_shape() -> None:
    assert _parse_tui_config(
        {
            "leader_key": "ctrl+space",
            "preferences": {
                "theme": {"name": "nord", "mode": "dark"},
                "reading": {"wrap": False, "sidebar_collapsed": True},
            },
        }
    ) == RuntimeTuiConfig(
        leader_key="ctrl+space",
        keymap=None,
        preferences=RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="nord", mode="dark"),
            reading=RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=True),
        ),
    )


def test_load_runtime_config_resolves_tui_preferences_from_workspace_over_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config_dir = tmp_path / "global-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_config_dir))
    user_runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_runtime_config_path().write_text(
        json.dumps(
            {
                "tui": {
                    "preferences": {
                        "theme": {"name": "nord", "mode": "dark"},
                        "reading": {"wrap": False, "sidebar_collapsed": True},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "tui": {
                    "preferences": {
                        "theme": {"name": "tokyo-night"},
                        "reading": {"wrap": True},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.tui is not None
    assert config.tui.preferences == RuntimeTuiPreferences(
        theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
        reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=True),
    )


def test_load_runtime_config_resolves_tui_preferences_from_global_when_workspace_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config_dir = tmp_path / "global-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_config_dir))
    user_runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_runtime_config_path().write_text(
        json.dumps(
            {
                "tui": {
                    "preferences": {
                        "theme": {"name": "gruvbox", "mode": "dark"},
                        "reading": {"wrap": False, "sidebar_collapsed": True},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.tui is not None
    assert config.tui.preferences == RuntimeTuiPreferences(
        theme=RuntimeTuiThemePreferences(name="gruvbox", mode="dark"),
        reading=RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=True),
    )


def test_load_runtime_config_uses_builtin_tui_preference_defaults_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))

    config = load_runtime_config(tmp_path, env={})

    assert config.tui is not None
    assert config.tui.preferences == RuntimeTuiPreferences(
        theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
        reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
    )


def test_load_runtime_config_inherits_global_leader_key_when_workspace_only_sets_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config_dir = tmp_path / "global-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_config_dir))
    user_runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_runtime_config_path().write_text(
        json.dumps({"tui": {"leader_key": "ctrl+space"}}),
        encoding="utf-8",
    )
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "tui": {
                    "preferences": {
                        "reading": {"wrap": False},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.tui is not None
    assert config.tui.leader_key == "ctrl+space"
    assert config.tui.preferences == RuntimeTuiPreferences(
        theme=RuntimeTuiThemePreferences(name="textual-dark", mode="auto"),
        reading=RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=False),
    )


def test_load_runtime_config_preserves_invalid_theme_name_and_defers_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "tui": {
                    "preferences": {
                        "theme": {"name": "unknown-theme", "mode": "dark"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_runtime_config(tmp_path, env={})

    assert config.tui is not None
    assert config.tui.preferences is not None
    assert config.tui.preferences.theme == RuntimeTuiThemePreferences(
        name="unknown-theme", mode="dark"
    )


def test_save_workspace_tui_preferences_preserves_unrelated_runtime_config_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "model": "opencode/gpt-5.4",
                "approval_mode": "ask",
                "tui": {"leader_key": "alt+x"},
            }
        ),
        encoding="utf-8",
    )

    save_workspace_tui_preferences(
        tmp_path,
        RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="nord", mode="dark"),
            reading=RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=True),
        ),
    )

    payload = json.loads(runtime_config_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["model"] == "opencode/gpt-5.4"
    assert payload["approval_mode"] == "ask"
    assert payload["tui"]["preferences"] == {
        "theme": {"name": "nord", "mode": "dark"},
        "reading": {"wrap": False, "sidebar_collapsed": True},
    }


def test_save_workspace_tui_preferences_preserves_tui_leader_key_and_keymap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "tui": {
                    "leader_key": "ctrl+space",
                    "keymap": {"n": "session_new"},
                }
            }
        ),
        encoding="utf-8",
    )

    save_workspace_tui_preferences(
        tmp_path,
        RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="gruvbox", mode="dark"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
        ),
    )

    payload = json.loads(runtime_config_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["tui"]["leader_key"] == "ctrl+space"
    assert payload["tui"]["keymap"] == {"n": "session_new"}
    assert payload["tui"]["preferences"] == {
        "theme": {"name": "gruvbox", "mode": "dark"},
        "reading": {"wrap": True, "sidebar_collapsed": False},
    }


def test_save_workspace_tui_preferences_writes_only_local_override_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "global-config"))
    runtime_config_path(tmp_path).write_text(
        json.dumps(
            {
                "tui": {
                    "preferences": {
                        "reading": {"sidebar_collapsed": True},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    save_workspace_tui_preferences(
        tmp_path,
        RuntimeTuiPreferences(
            reading=RuntimeTuiReadingPreferences(wrap=False),
        ),
    )

    payload = json.loads(runtime_config_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["tui"]["preferences"] == {"reading": {"wrap": False}}


def test_save_global_tui_preferences_writes_user_config_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config_dir = tmp_path / "global-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_config_dir))

    save_global_tui_preferences(
        RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="tokyo-night", mode="dark"),
            reading=RuntimeTuiReadingPreferences(wrap=False, sidebar_collapsed=True),
        )
    )

    payload = json.loads(user_runtime_config_path().read_text(encoding="utf-8"))
    assert payload == {
        "tui": {
            "preferences": {
                "theme": {"name": "tokyo-night", "mode": "dark"},
                "reading": {"wrap": False, "sidebar_collapsed": True},
            }
        }
    }


def test_save_global_tui_preferences_preserves_unrelated_global_config_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_config_dir = tmp_path / "global-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(global_config_dir))
    user_runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    user_runtime_config_path().write_text(
        json.dumps({"model": "opencode/gpt-5.4", "tui": {"leader_key": "alt+x"}}),
        encoding="utf-8",
    )

    save_global_tui_preferences(
        RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="textual-light", mode="light"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
        )
    )

    payload = json.loads(user_runtime_config_path().read_text(encoding="utf-8"))
    assert payload["model"] == "opencode/gpt-5.4"
    assert payload["tui"]["leader_key"] == "alt+x"
    assert payload["tui"]["preferences"] == {
        "theme": {"name": "textual-light", "mode": "light"},
        "reading": {"wrap": True, "sidebar_collapsed": False},
    }


def test_effective_runtime_tui_preferences_resolves_invalid_theme_name_to_mode_default() -> None:
    effective = effective_runtime_tui_preferences(
        RuntimeTuiPreferences(
            theme=RuntimeTuiThemePreferences(name="unknown-theme", mode="light"),
            reading=RuntimeTuiReadingPreferences(wrap=True, sidebar_collapsed=False),
        )
    )

    assert effective.theme == RuntimeTuiThemePreferences(name="textual-light", mode="light")


def test_parse_tui_config_preserves_valid_leader_key_and_keymap() -> None:
    assert _parse_tui_config(
        {
            "leader_key": "ctrl+space",
            "keymap": {
                "n": "session_new",
                "r": "session_resume",
                "p": "command_palette",
            },
        }
    ) == RuntimeTuiConfig(
        leader_key="ctrl+space",
        keymap={
            "n": "session_new",
            "r": "session_resume",
            "p": "command_palette",
        },
    )


@pytest.mark.parametrize(
    ("raw_value", "match"),
    [
        pytest.param([], "runtime config field 'tui'", id="shape"),
        pytest.param(
            {"leader_key": 3},
            "runtime config field 'tui.leader_key'",
            id="leader-key-type",
        ),
        pytest.param(
            {"keymap": []},
            "runtime config field 'tui.keymap'",
            id="keymap-shape",
        ),
        pytest.param(
            {"keymap": {"n": False}},
            "runtime config field 'tui.keymap' values must be strings",
            id="keymap-value-type",
        ),
        pytest.param(
            {"keymap": {1: "session_new"}},
            "runtime config field 'tui.keymap' keys must be strings",
            id="keymap-key-type",
        ),
        pytest.param(
            {"keymap": {"n": "quit"}},
            "runtime config field 'tui.keymap' values must be one of: "
            "command_palette, session_new, session_resume",
            id="keymap-value-enum",
        ),
        pytest.param(
            {"preferences": []},
            "runtime config field 'tui.preferences'",
            id="preferences-shape",
        ),
        pytest.param(
            {"preferences": {"theme": []}},
            "runtime config field 'tui.preferences.theme'",
            id="preferences-theme-shape",
        ),
        pytest.param(
            {"preferences": {"theme": {"mode": "sepia"}}},
            "runtime config field 'tui.preferences.theme.mode'",
            id="preferences-theme-mode-invalid",
        ),
        pytest.param(
            {"preferences": {"reading": []}},
            "runtime config field 'tui.preferences.reading'",
            id="preferences-reading-shape",
        ),
        pytest.param(
            {"preferences": {"reading": {"wrap": "yes"}}},
            "runtime config field 'tui.preferences.reading.wrap'",
            id="preferences-reading-wrap-type",
        ),
        pytest.param(
            {"preferences": {"reading": {"sidebar_collapsed": "yes"}}},
            "runtime config field 'tui.preferences.reading.sidebar_collapsed'",
            id="preferences-reading-sidebar-collapsed-type",
        ),
    ],
)
def test_parse_tui_config_rejects_invalid_shapes_and_values(raw_value: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _ = _parse_tui_config(raw_value)


def test_parse_simple_extension_configs_preserve_public_dataclasses() -> None:
    assert _parse_tools_config({"builtin": {"enabled": True}, "paths": [".voidcode/tools"]}) == (
        RuntimeToolsConfig(
            builtin=RuntimeToolsBuiltinConfig(enabled=True),
            paths=(".voidcode/tools",),
        )
    )
    assert _parse_skills_config({"enabled": False, "paths": [".voidcode/skills"]}) == (
        RuntimeSkillsConfig(enabled=False, paths=(".voidcode/skills",))
    )
    assert _parse_acp_config({"enabled": True}) == RuntimeAcpConfig(enabled=True)


def test_runtime_config_uses_repo_local_filename_inside_workspace(tmp_path: Path) -> None:
    config_file = tmp_path / RUNTIME_CONFIG_FILE_NAME

    assert runtime_config_path(tmp_path) == config_file
