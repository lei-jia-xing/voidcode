from __future__ import annotations

from dataclasses import dataclass

from .models import SkillManifest, SkillManifestFrontmatter

FRONTMATTER_DELIMITER = "---"
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
    lines = contents.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise SkillManifestParseError("skill file must begin with a simplified frontmatter block")

    parsed: dict[str, str] = {}
    for index, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if line == FRONTMATTER_DELIMITER:
            break
        if not line:
            continue
        key, separator, value = raw_line.partition(":")
        if separator != ":":
            raise SkillManifestParseError(
                f"skill frontmatter line {index} must use 'key: value' syntax"
            )
        normalized_key = key.strip()
        if normalized_key not in SUPPORTED_FRONTMATTER_KEYS:
            raise SkillManifestParseError(f"unsupported skill frontmatter key: {normalized_key}")
        normalized_value = value.strip()
        if not normalized_value:
            raise SkillManifestParseError(
                f"skill frontmatter field '{normalized_key}' must not be empty"
            )
        parsed[normalized_key] = normalized_value
    else:
        raise SkillManifestParseError("skill frontmatter must terminate with a closing '---' line")

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
    lines = contents.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise SkillManifestParseError(
            "skill file must begin with a simplified frontmatter block",
            path=path,
        )

    for index, raw_line in enumerate(lines[1:], start=2):
        if raw_line.strip() == FRONTMATTER_DELIMITER:
            return "\n".join(lines[index:]).strip()

    raise SkillManifestParseError(
        "skill frontmatter must terminate with a closing '---' line",
        path=path,
    )


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
