from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.lsp import (
    LspServerConfigOverride,
    builtin_lsp_server_presets,
    derive_workspace_lsp_defaults,
    match_lsp_servers_for_path,
    resolve_lsp_server_config,
    resolve_lsp_server_configs,
)


def test_builtin_lsp_preset_catalog_covers_common_languages() -> None:
    preset_ids = {preset.id for preset in builtin_lsp_server_presets()}

    assert len(preset_ids) >= 20
    assert {
        "pyright",
        "clangd",
        "gopls",
        "rust-analyzer",
        "tsserver",
        "jdtls",
        "lua_ls",
        "yamlls",
        "bashls",
        "csharp-ls",
        "tailwindcss",
    }.issubset(preset_ids)


def test_resolve_lsp_server_config_uses_builtin_preset_by_server_name() -> None:
    config = resolve_lsp_server_config("pyright", LspServerConfigOverride())

    assert config.id == "pyright"
    assert config.preset == "pyright"
    assert config.command == ("pyright-langserver", "--stdio")
    assert config.extensions == (".py", ".pyi")
    assert config.languages == ("python",)
    assert config.matches_path(Path("sample.py")) is True


def test_resolve_lsp_server_config_merges_builtin_preset_with_project_override() -> None:
    config = resolve_lsp_server_config(
        "python",
        LspServerConfigOverride(
            preset="pyright",
            command=("custom-pyright", "--stdio"),
            extensions=(".pyw",),
            root_markers=("requirements-dev.txt",),
            settings={"python": {"analysis": {"typeCheckingMode": "strict"}}},
            init_options={"diagnostics": {"enable": True}},
        ),
    )

    assert config.id == "python"
    assert config.preset == "pyright"
    assert config.command == ("custom-pyright", "--stdio")
    assert config.extensions == (".py", ".pyi", ".pyw")
    assert "requirements-dev.txt" in config.root_markers
    assert config.settings == {"python": {"analysis": {"typeCheckingMode": "strict"}}}
    assert config.init_options == {"diagnostics": {"enable": True}}


def test_resolve_lsp_server_config_deep_merges_settings() -> None:
    config = resolve_lsp_server_config(
        "pyright",
        LspServerConfigOverride(
            settings={
                "python": {
                    "analysis": {
                        "typeCheckingMode": "strict",
                    }
                }
            }
        ),
    )

    assert config.settings == {
        "python": {"analysis": {"typeCheckingMode": "strict"}},
    }


def test_resolve_lsp_server_config_supports_builtin_catalog_entry_for_clangd() -> None:
    config = resolve_lsp_server_config("clangd", LspServerConfigOverride())

    assert config.command == ("clangd",)
    assert config.languages == ("c", "cpp", "objective-c", "objective-cpp")
    assert config.matches_path(Path("main.cpp")) is True


def test_resolve_lsp_server_config_supports_tailwindcss_preset() -> None:
    config = resolve_lsp_server_config("tailwindcss", LspServerConfigOverride())

    assert config.command == ("tailwindcss-language-server", "--stdio")
    assert "tailwindcss" in config.languages
    assert "typescriptreact" in config.languages
    assert "tailwind.config.ts" in config.root_markers
    assert config.matches_path(Path("component.tsx")) is True
    assert config.matches_path(Path("style.css")) is True


def test_resolve_lsp_server_configs_matches_servers_by_extension() -> None:
    servers = resolve_lsp_server_configs(
        {
            "pyright": LspServerConfigOverride(),
            "gopls": LspServerConfigOverride(),
        }
    )

    assert match_lsp_servers_for_path(servers, Path("main.py")) == ("pyright",)
    assert match_lsp_servers_for_path(servers, Path("main.go")) == ("gopls",)


def test_resolve_lsp_server_config_matches_canonical_dockerfile_name() -> None:
    config = resolve_lsp_server_config("dockerls", LspServerConfigOverride())

    assert config.matches_path(Path("Dockerfile")) is True


def test_resolve_lsp_server_configs_prefers_dockerls_for_canonical_dockerfile_name() -> None:
    servers = resolve_lsp_server_configs(
        {
            "yamlls": LspServerConfigOverride(),
            "dockerls": LspServerConfigOverride(),
        }
    )

    assert match_lsp_servers_for_path(servers, Path("Dockerfile")) == ("dockerls",)


def test_resolve_lsp_server_config_rejects_unknown_preset() -> None:
    with pytest.raises(ValueError, match="unknown LSP preset"):
        _ = resolve_lsp_server_config(
            "python",
            LspServerConfigOverride(preset="not-real"),
        )


def test_resolve_lsp_server_config_requires_command_for_unknown_server() -> None:
    with pytest.raises(ValueError, match="must define a command"):
        _ = resolve_lsp_server_config(
            "custom-python",
            LspServerConfigOverride(),
        )


def test_derive_workspace_lsp_defaults_detects_python_workspace(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "pyright-langserver",
    )

    assert derived == {"pyright": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_detects_typescript_workspace(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "typescript-language-server",
    )

    assert derived == {"tsserver": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_skips_missing_executable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(tmp_path, executable_exists=lambda command: False)

    assert derived == {}


def test_derive_workspace_lsp_defaults_detects_go_workspace(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "gopls",
    )

    assert derived == {"gopls": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_detects_rust_workspace(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "rust-analyzer",
    )

    assert derived == {"rust-analyzer": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_detects_clangd_workspace(tmp_path: Path) -> None:
    (tmp_path / "compile_commands.json").write_text("[]\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "clangd",
    )

    assert derived == {"clangd": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_detects_java_workspace(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "jdtls",
    )

    assert derived == {"jdtls": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_detects_lua_workspace(tmp_path: Path) -> None:
    (tmp_path / ".luarc.json").write_text("{}\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "lua-language-server",
    )

    assert derived == {"lua_ls": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_detects_zig_workspace(tmp_path: Path) -> None:
    (tmp_path / "build.zig").write_text("pub fn build() void {}\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "zls",
    )

    assert derived == {"zls": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_detects_csharp_workspace(tmp_path: Path) -> None:
    (tmp_path / "global.json").write_text("{}\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command == "csharp-ls",
    )

    assert derived == {"csharp-ls": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_does_not_enable_ruff_for_python_workspace(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: command in {"pyright-langserver", "ruff"},
    )

    assert derived == {"pyright": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_does_not_enable_web_auxiliary_servers_from_package_json(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: (
            command
            in {
                "typescript-language-server",
                "vscode-eslint-language-server",
                "vscode-html-language-server",
                "vscode-css-language-server",
                "tailwindcss-language-server",
                "vue-language-server",
            }
        ),
    )

    assert derived == {"tsserver": LspServerConfigOverride()}


def test_derive_workspace_lsp_defaults_does_not_enable_git_only_presets(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    derived = derive_workspace_lsp_defaults(
        tmp_path,
        executable_exists=lambda command: (
            command
            in {
                "yaml-language-server",
                "bash-language-server",
                "marksman",
                "vscode-json-language-server",
            }
        ),
    )

    assert derived == {}
