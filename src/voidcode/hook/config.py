from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _empty_formatter_presets() -> dict[str, RuntimeFormatterPresetConfig]:
    return {
        "python": RuntimeFormatterPresetConfig(command=("ruff", "format")),
        "typescript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "javascript": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "json": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "markdown": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "yaml": RuntimeFormatterPresetConfig(command=("prettier", "--write")),
        "rust": RuntimeFormatterPresetConfig(command=("rustfmt",)),
        "go": RuntimeFormatterPresetConfig(command=("gofmt",)),
    }


_LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx"),
    "json": (".json",),
    "markdown": (".md",),
    "yaml": (".yaml", ".yml"),
    "rust": (".rs",),
    "go": (".go",),
}


@dataclass(frozen=True, slots=True)
class RuntimeFormatterPresetConfig:
    command: tuple[str, ...]


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
            exts = _LANGUAGE_EXTENSIONS.get(lang, ())
            if any(file_path.name.endswith(ext) for ext in exts):
                return lang, preset
        return None