from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

SKILL_ENTRY_FILE_NAME = "SKILL.md"
DEFAULT_SKILL_SEARCH_PATHS = (".voidcode/skills",)
_FRONTMATTER_DELIMITER = "---"
_SUPPORTED_FRONTMATTER_KEYS = frozenset({"name", "description"})


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    directory: Path
    entry_path: Path


@dataclass(slots=True)
class SkillRegistry:
    skills: dict[str, SkillMetadata] = field(default_factory=dict)

    @classmethod
    def from_skills(cls, skills: Iterable[SkillMetadata]) -> SkillRegistry:
        return cls(skills={skill.name: skill for skill in skills})

    @classmethod
    def discover(
        cls,
        *,
        workspace: Path,
        search_paths: Iterable[str] = DEFAULT_SKILL_SEARCH_PATHS,
        loader: LocalSkillMetadataLoader | None = None,
    ) -> SkillRegistry:
        metadata_loader = loader or LocalSkillMetadataLoader()
        return cls.from_skills(
            metadata_loader.discover(workspace=workspace, search_paths=search_paths)
        )

    def all(self) -> tuple[SkillMetadata, ...]:
        return tuple(self.skills.values())

    def resolve(self, skill_name: str) -> SkillMetadata:
        try:
            return self.skills[skill_name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {skill_name}") from exc


class LocalSkillMetadataLoader:
    def discover(
        self,
        *,
        workspace: Path,
        search_paths: Iterable[str] = DEFAULT_SKILL_SEARCH_PATHS,
    ) -> tuple[SkillMetadata, ...]:
        resolved_workspace = workspace.resolve()
        discovered: list[SkillMetadata] = []

        for search_path in search_paths:
            skill_root = _resolve_workspace_relative_path(
                workspace=resolved_workspace, configured_path=search_path
            )
            if not skill_root.exists() or not skill_root.is_dir():
                continue
            for skill_dir in sorted(path for path in skill_root.iterdir() if path.is_dir()):
                entry_path = skill_dir / SKILL_ENTRY_FILE_NAME
                if not entry_path.exists() or not entry_path.is_file():
                    continue
                discovered.append(self.load(entry_path))

        return tuple(discovered)

    def load(self, entry_path: Path) -> SkillMetadata:
        resolved_entry_path = entry_path.resolve()
        metadata = parse_skill_frontmatter(resolved_entry_path.read_text(encoding="utf-8"))
        return SkillMetadata(
            name=metadata["name"],
            description=metadata["description"],
            directory=resolved_entry_path.parent,
            entry_path=resolved_entry_path,
        )


def parse_skill_frontmatter(contents: str) -> dict[str, str]:
    lines = contents.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise ValueError("skill file must begin with a simplified frontmatter block")

    parsed: dict[str, str] = {}
    for index, raw_line in enumerate(lines[1:], start=2):
        line = raw_line.strip()
        if line == _FRONTMATTER_DELIMITER:
            break
        if not line:
            continue
        key, separator, value = raw_line.partition(":")
        if separator != ":":
            raise ValueError(f"skill frontmatter line {index} must use 'key: value' syntax")
        normalized_key = key.strip()
        if normalized_key not in _SUPPORTED_FRONTMATTER_KEYS:
            raise ValueError(f"unsupported skill frontmatter key: {normalized_key}")
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError(f"skill frontmatter field '{normalized_key}' must not be empty")
        parsed[normalized_key] = normalized_value
    else:
        raise ValueError("skill frontmatter must terminate with a closing '---' line")

    missing_keys = _SUPPORTED_FRONTMATTER_KEYS.difference(parsed)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise ValueError(f"skill frontmatter missing required fields: {missing}")
    return parsed


def _resolve_workspace_relative_path(*, workspace: Path, configured_path: str) -> Path:
    candidate_path = Path(configured_path)
    if candidate_path.is_absolute():
        return candidate_path.resolve()

    resolved_path = (workspace / candidate_path).resolve()
    try:
        resolved_path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"skill search path escapes workspace: {configured_path}") from exc
    return resolved_path
