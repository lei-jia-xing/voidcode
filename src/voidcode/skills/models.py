from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeIs

SkillOrigin = Literal["workspace", "builtin"]


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


def is_skill_origin(value: object) -> TypeIs[SkillOrigin]:
    """Whether an untrusted ``origin`` token names one of the skill origins."""
    return value == "workspace" or value == "builtin"


def _validated_skill_origin(value: object) -> SkillOrigin:
    if is_skill_origin(value):
        return value
    raise ValueError("origin must be one of: workspace, builtin")


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    content: str
    directory: Path
    entry_path: Path
    origin: SkillOrigin = "workspace"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validated_non_empty_string(self.name, field_name="name"))
        object.__setattr__(
            self,
            "description",
            _validated_non_empty_string(self.description, field_name="description"),
        )
        object.__setattr__(
            self,
            "content",
            _validated_string(self.content, field_name="content"),
        )
        directory = _validated_path(self.directory, field_name="directory")
        entry_path = _validated_path(self.entry_path, field_name="entry_path")
        origin = _validated_skill_origin(self.origin)
        object.__setattr__(self, "directory", directory)
        object.__setattr__(self, "entry_path", entry_path)
        object.__setattr__(self, "origin", origin)
        if entry_path.parent != directory:
            raise ValueError("entry_path must live inside directory")
