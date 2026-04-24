from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def _validated_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized


def _validated_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _validated_path(value: object, *, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise ValueError(f"{field_name} must be a pathlib.Path")
    return value


@dataclass(frozen=True, slots=True)
class SkillManifestFrontmatter:
    name: str
    description: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validated_non_empty_string(self.name, field_name="name"))
        object.__setattr__(
            self,
            "description",
            _validated_non_empty_string(self.description, field_name="description"),
        )


@dataclass(frozen=True, slots=True)
class SkillManifest(SkillManifestFrontmatter):
    content: str

    def __post_init__(self) -> None:
        SkillManifestFrontmatter.__post_init__(self)
        object.__setattr__(
            self,
            "content",
            _validated_string(self.content, field_name="content"),
        )


@dataclass(frozen=True, slots=True)
class SkillMetadata(SkillManifest):
    directory: Path
    entry_path: Path

    def __post_init__(self) -> None:
        SkillManifest.__post_init__(self)
        directory = _validated_path(self.directory, field_name="directory")
        entry_path = _validated_path(self.entry_path, field_name="entry_path")
        object.__setattr__(self, "directory", directory)
        object.__setattr__(self, "entry_path", entry_path)
        if entry_path.parent != directory:
            raise ValueError("entry_path must live inside directory")
