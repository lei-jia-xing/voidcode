from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import DEFAULT_SKILL_SEARCH_PATHS, LocalSkillMetadataLoader
from .models import SkillMetadata


@dataclass(slots=True)
class SkillRegistry:
    skills: dict[str, SkillMetadata] = field(default_factory=dict)

    @classmethod
    def from_skills(cls, skills: Iterable[SkillMetadata]) -> SkillRegistry:
        resolved: dict[str, SkillMetadata] = {}
        for skill in skills:
            existing = resolved.get(skill.name)
            if existing is not None:
                raise ValueError(
                    "duplicate skill name "
                    f"'{skill.name}' discovered at {existing.entry_path} and {skill.entry_path}"
                )
            resolved[skill.name] = skill
        return cls(skills=resolved)

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
