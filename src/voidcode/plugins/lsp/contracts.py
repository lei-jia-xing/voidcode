from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class LspServerPreset:
    id: str
    command: tuple[str, ...]
    extensions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    root_markers: tuple[str, ...] = ()
    settings: dict[str, object] = field(default_factory=dict)
    init_options: dict[str, object] = field(default_factory=dict)


class LspServerConfigOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    preset: str | None = Field(default=None, min_length=1)
    command: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    root_markers: list[str] = Field(default_factory=list)
    settings: dict[str, object] = Field(default_factory=dict)
    init_options: dict[str, object] = Field(default_factory=dict)


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
        normalized_name = path.name.lower()
        normalized_suffix = path.suffix.lower()
        return any(normalized_suffix == extension or normalized_name == extension.removeprefix(".") for extension in self.extensions)
