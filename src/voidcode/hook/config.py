from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


def _empty_formatter_presets() -> dict[str, RuntimeFormatterPresetConfig]:
    return {}


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
