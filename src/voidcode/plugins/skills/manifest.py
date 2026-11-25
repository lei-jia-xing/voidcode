"""``SKILL.md`` identity, metadata validation and manifest parsing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from voidcode.capabilities.frontmatter import load_frontmatter_mapping, split_frontmatter

from .limits import MAX_SKILL_BODY_BYTES, MAX_SKILL_DESCRIPTION_CHARS, MAX_SKILL_NAME_CHARS

SKILL_ENTRY_FILE_NAME = "SKILL.md"
SKILL_URI_PREFIX = "skill://"
SUPPORTED_FRONTMATTER_KEYS: frozenset[str] = frozenset({"name", "description"})


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
class SkillMetadata:
    name: str
    description: str
    content: str
    directory: Path
    entry_path: Path
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validated_non_empty_string(self.name, field_name="name"))
        object.__setattr__(self, "description", _validated_non_empty_string(self.description, field_name="description"))
        object.__setattr__(self, "content", _validated_string(self.content, field_name="content"))
        directory = _validated_path(self.directory, field_name="directory")
        entry_path = _validated_path(self.entry_path, field_name="entry_path")
        object.__setattr__(self, "directory", directory)
        object.__setattr__(self, "entry_path", entry_path)
        object.__setattr__(self, "enabled", bool(self.enabled))
        if entry_path.parent != directory:
            raise ValueError("entry_path must live inside directory")
        if len(self.name) > MAX_SKILL_NAME_CHARS:
            raise ValueError(f"skill name must not exceed {MAX_SKILL_NAME_CHARS} characters (got {len(self.name)})")
        if len(self.description) > MAX_SKILL_DESCRIPTION_CHARS:
            raise ValueError(f"skill description must not exceed {MAX_SKILL_DESCRIPTION_CHARS} characters (got {len(self.description)})")
        if len(self.content.encode("utf-8")) > MAX_SKILL_BODY_BYTES:
            raise ValueError(f"skill body must not exceed {MAX_SKILL_BODY_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class SkillManifestParseError(ValueError):
    message: str
    path: str | None = None

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.message}"
        return self.message


def _parse_skill_frontmatter_fields(payload: Mapping[str, object]) -> dict[str, str]:
    for key in payload:
        if key not in SUPPORTED_FRONTMATTER_KEYS:
            raise SkillManifestParseError(f"unsupported skill frontmatter key: {key}")

    parsed: dict[str, str] = {}
    for key in sorted(SUPPORTED_FRONTMATTER_KEYS):
        if key not in payload:
            continue
        value = payload[key]
        parsed[key] = _validated_non_empty_string(value, field_name=f"skill frontmatter field '{key}'")

    missing_keys = SUPPORTED_FRONTMATTER_KEYS.difference(parsed)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise SkillManifestParseError(f"skill frontmatter missing required fields: {missing}")
    return parsed


def parse_skill_manifest(contents: str, *, entry_path: Path) -> SkillMetadata:
    try:
        raw_frontmatter, body = split_frontmatter(contents)
        parsed = _parse_skill_frontmatter_fields(load_frontmatter_mapping(raw_frontmatter))
        # split_frontmatter strips surrounding whitespace; content is that stripped body.
        _validated_non_empty_string(body, field_name="content")
        return SkillMetadata(
            name=parsed["name"],
            description=parsed["description"],
            content=body,
            directory=entry_path.parent,
            entry_path=entry_path,
        )
    except ValueError as exc:
        if isinstance(exc, SkillManifestParseError):
            raise SkillManifestParseError(exc.message, path=str(entry_path)) from exc
        raise SkillManifestParseError(str(exc), path=str(entry_path)) from exc
