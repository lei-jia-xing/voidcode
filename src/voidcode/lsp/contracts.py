from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LspServerPreset:
    id: str
    command: tuple[str, ...]
    extensions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    root_markers: tuple[str, ...] = ()
    settings: dict[str, object] = field(default_factory=dict)
    init_options: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LspServerConfigOverride:
    preset: str | None = None
    command: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    root_markers: tuple[str, ...] = ()
    settings: dict[str, object] = field(default_factory=dict)
    init_options: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolvedLspServerConfig:
    id: str
    command: tuple[str, ...]
    extensions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    root_markers: tuple[str, ...] = ()
    settings: dict[str, object] = field(default_factory=dict)
    init_options: dict[str, object] = field(default_factory=dict)
    preset: str | None = None

    def matches_path(self, path: Path) -> bool:
        if not self.extensions:
            return False
        return path.suffix.lower() in self.extensions
