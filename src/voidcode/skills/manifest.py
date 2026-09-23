from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..frontmatter import load_frontmatter_mapping, split_frontmatter
from .models import SkillMetadata

SUPPORTED_FRONTMATTER_KEYS = frozenset({"name", "description"})


@dataclass(frozen=True, slots=True)
class SkillManifestParseError(ValueError):
    message: str
    path: str | None = None

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.message}"
        return self.message


def _parse_skill_frontmatter_fields(contents: str) -> dict[str, str]:
    raw_frontmatter, _body = split_frontmatter(contents)
    payload = load_frontmatter_mapping(raw_frontmatter)

    for key in payload:
        if key not in SUPPORTED_FRONTMATTER_KEYS:
            raise SkillManifestParseError(f"unsupported skill frontmatter key: {key}")

    parsed: dict[str, str] = {}
    for key in sorted(SUPPORTED_FRONTMATTER_KEYS):
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            raise SkillManifestParseError(f"skill frontmatter field '{key}' must be a non-empty string")
        parsed[key] = value.strip()

    missing_keys = SUPPORTED_FRONTMATTER_KEYS.difference(parsed)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise SkillManifestParseError(f"skill frontmatter missing required fields: {missing}")
    return parsed


def parse_skill_manifest(contents: str, *, entry_path: Path) -> SkillMetadata:
    try:
        parsed = _parse_skill_frontmatter_fields(contents)
        _raw_frontmatter, body = split_frontmatter(contents)
        if not body.strip():
            raise ValueError("content must be a non-empty string")
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
