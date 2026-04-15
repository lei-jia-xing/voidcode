from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

type FormatterCwdPolicy = Literal["workspace", "nearest_root", "file_directory"]


def _empty_formatter_presets() -> dict[str, RuntimeFormatterPresetConfig]:
    return {
        "python": RuntimeFormatterPresetConfig(
            command=("ruff", "format"),
            extensions=(".py", ".pyi"),
            root_markers=("pyproject.toml", "ruff.toml", ".ruff.toml"),
            fallback_commands=(("uvx", "ruff", "format"), ("python", "-m", "ruff", "format")),
        ),
        "typescript": RuntimeFormatterPresetConfig(
            command=("prettier", "--write"),
            extensions=(".ts", ".tsx", ".mts", ".cts"),
            root_markers=(
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
            ),
            fallback_commands=(
                ("bunx", "prettier", "--write"),
                ("pnpm", "exec", "prettier", "--write"),
                ("npx", "prettier", "--write"),
            ),
        ),
        "javascript": RuntimeFormatterPresetConfig(
            command=("prettier", "--write"),
            extensions=(".js", ".jsx", ".mjs", ".cjs"),
            root_markers=(
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
            ),
            fallback_commands=(
                ("bunx", "prettier", "--write"),
                ("pnpm", "exec", "prettier", "--write"),
                ("npx", "prettier", "--write"),
            ),
        ),
        "json": RuntimeFormatterPresetConfig(
            command=("prettier", "--write"),
            extensions=(".json", ".jsonc"),
            root_markers=(
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
            ),
            fallback_commands=(
                ("bunx", "prettier", "--write"),
                ("pnpm", "exec", "prettier", "--write"),
                ("npx", "prettier", "--write"),
            ),
        ),
        "markdown": RuntimeFormatterPresetConfig(
            command=("prettier", "--write"),
            extensions=(".md", ".mdx"),
            root_markers=(
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
            ),
            fallback_commands=(
                ("bunx", "prettier", "--write"),
                ("pnpm", "exec", "prettier", "--write"),
                ("npx", "prettier", "--write"),
            ),
        ),
        "yaml": RuntimeFormatterPresetConfig(
            command=("prettier", "--write"),
            extensions=(".yaml", ".yml"),
            root_markers=(
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
            ),
            fallback_commands=(
                ("bunx", "prettier", "--write"),
                ("pnpm", "exec", "prettier", "--write"),
                ("npx", "prettier", "--write"),
            ),
        ),
        "rust": RuntimeFormatterPresetConfig(
            command=("rustfmt",),
            extensions=(".rs",),
            root_markers=("Cargo.toml", "rustfmt.toml", ".rustfmt.toml"),
        ),
        "go": RuntimeFormatterPresetConfig(
            command=("gofmt",),
            extensions=(".go",),
            root_markers=("go.mod",),
        ),
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
