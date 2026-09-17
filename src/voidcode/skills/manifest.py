from __future__ import annotations

from dataclasses import dataclass

from ..frontmatter import load_frontmatter_mapping, split_frontmatter
from .models import SkillManifest, SkillManifestFrontmatter

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


def parse_skill_frontmatter(
    contents: str,
    *,
    path: str | None = None,
) -> SkillManifestFrontmatter:
    try:
        parsed = _parse_skill_frontmatter_fields(contents)
        return SkillManifestFrontmatter(
            name=parsed["name"],
            description=parsed["description"],
        )
    except ValueError as exc:
        if isinstance(exc, SkillManifestParseError):
            raise SkillManifestParseError(exc.message, path=path) from exc
        raise SkillManifestParseError(str(exc), path=path) from exc


def parse_skill_body(contents: str, *, path: str | None = None) -> str:
    try:
        _raw_frontmatter, body = split_frontmatter(contents)
    except ValueError as exc:
        raise SkillManifestParseError(str(exc), path=path) from exc
    return body


def parse_skill_manifest(contents: str, *, path: str | None = None) -> SkillManifest:
    frontmatter = parse_skill_frontmatter(contents, path=path)
    body = parse_skill_body(contents, path=path)
    try:
        if not body.strip():
            raise ValueError("content must be a non-empty string")
        return SkillManifest(
            name=frontmatter.name,
            description=frontmatter.description,
            content=body,
        )
    except ValueError as exc:
        raise SkillManifestParseError(str(exc), path=path) from exc
