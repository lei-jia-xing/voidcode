from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

type FormatterCwdPolicy = Literal["workspace", "nearest_root", "file_directory"]

_PRETTIER_ROOT_MARKERS = (
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

_PRETTIER_FALLBACK_COMMANDS = (
    ("bunx", "prettier", "--write"),
    ("pnpm", "exec", "prettier", "--write"),
    ("npx", "prettier", "--write"),
)


def _prettier_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("prettier", "--write"),
        extensions=extensions,
        root_markers=_PRETTIER_ROOT_MARKERS,
        fallback_commands=_PRETTIER_FALLBACK_COMMANDS,
    )


def _shfmt_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("shfmt", "-w"),
        extensions=extensions,
        root_markers=(".editorconfig", ".shfmt.conf", ".shfmt"),
    )


def _dockerfmt_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("dockerfmt", "--write"),
        extensions=extensions,
        root_markers=(".dockerfmt.toml", ".dockerfmt.hcl", "Dockerfile"),
    )


def _clang_format_preset(*extensions: str) -> RuntimeFormatterPresetConfig:
    return RuntimeFormatterPresetConfig(
        command=("clang-format", "-i"),
        extensions=extensions,
        root_markers=(".clang-format", "_clang-format", "compile_commands.json", "CMakeLists.txt"),
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
    )


def _empty_formatter_presets() -> dict[str, RuntimeFormatterPresetConfig]:
    return {
        "python": RuntimeFormatterPresetConfig(
            command=("ruff", "format"),
            extensions=(".py", ".pyi"),
            root_markers=("pyproject.toml", "ruff.toml", ".ruff.toml"),
            fallback_commands=(("uvx", "ruff", "format"), ("python", "-m", "ruff", "format")),
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
        ),
        "shell": _shfmt_preset(".sh", ".bash", ".zsh"),
        "dockerfile": _dockerfmt_preset("Dockerfile"),
        "nix": RuntimeFormatterPresetConfig(
            command=("nixfmt",),
            extensions=(".nix",),
            root_markers=("flake.nix", "shell.nix", "default.nix"),
        ),
        "sql": _sql_formatter_preset(),
        "rust": RuntimeFormatterPresetConfig(
            command=("rustfmt",),
            extensions=(".rs",),
            root_markers=("Cargo.toml", "rustfmt.toml", ".rustfmt.toml"),
        ),
        "go": RuntimeFormatterPresetConfig(
            command=("gofmt", "-w"),
            extensions=(".go",),
            root_markers=("go.mod",),
        ),
        "c": _clang_format_preset(".c", ".h"),
        "cpp": _clang_format_preset(".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"),
        "java": RuntimeFormatterPresetConfig(
            command=("google-java-format", "--replace"),
            extensions=(".java",),
            root_markers=(".google-java-format", "pom.xml", "build.gradle", "build.gradle.kts"),
        ),
        "kotlin": RuntimeFormatterPresetConfig(
            command=("ktlint", "-F"),
            extensions=(".kt", ".kts"),
            root_markers=("ktlint.yml", ".editorconfig", "build.gradle.kts"),
        ),
        "xml": _prettier_preset(".xml"),
    }


@dataclass(frozen=True, slots=True)
class RuntimeFormatterPresetConfig:
    command: tuple[str, ...]
    extensions: tuple[str, ...] = ()
    root_markers: tuple[str, ...] = ()
    fallback_commands: tuple[tuple[str, ...], ...] = ()
    cwd_policy: FormatterCwdPolicy = "nearest_root"

    def matches_file(self, file_path: Path) -> bool:
        normalized_name = file_path.name.lower()
        return any(normalized_name.endswith(extension.lower()) for extension in self.extensions)


@dataclass(frozen=True, slots=True)
class RuntimeHooksConfig:
    enabled: bool | None = None
    pre_tool: tuple[tuple[str, ...], ...] = ()
    post_tool: tuple[tuple[str, ...], ...] = ()
    formatter_presets: Mapping[str, RuntimeFormatterPresetConfig] = field(
        default_factory=_empty_formatter_presets
    )

    def resolve_formatter(self, file_path: Path) -> tuple[str, RuntimeFormatterPresetConfig] | None:
        for lang, preset in self.formatter_presets.items():
            if preset.matches_file(file_path):
                return lang, preset
        return None
