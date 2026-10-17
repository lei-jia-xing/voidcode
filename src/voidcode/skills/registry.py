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
                raise ValueError(f"duplicate skill name '{skill.name}' discovered at {existing.entry_path} and {skill.entry_path}")
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
        return cls.from_skills(metadata_loader.discover(workspace=workspace, search_paths=search_paths))

    def all(self) -> tuple[SkillMetadata, ...]:
        return tuple(self.skills.values())

    def enabled_skills(self) -> tuple[SkillMetadata, ...]:
        return tuple(skill for skill in self.skills.values() if skill.enabled)

    def is_enabled(self, skill_name: str) -> bool:
        skill = self.skills.get(skill_name)
        return skill is not None and skill.enabled

    def apply_config_rules(self, rules: Iterable[object]) -> None:
        from dataclasses import replace

        for rule in rules:
            name = getattr(rule, "name", None)
            path = getattr(rule, "path", None)
            enabled = bool(getattr(rule, "enabled", True))
            if name and name in self.skills:
                self.skills[name] = replace(self.skills[name], enabled=enabled)
            if path:
                try:
                    target_path = Path(path).resolve()
                except Exception:
                    target_path = Path(path)
                for skill_name, skill in list(self.skills.items()):
                    if skill.entry_path.resolve() == target_path or skill.directory.resolve() == target_path:
                        self.skills[skill_name] = replace(skill, enabled=enabled)

    def resolve(self, skill_name: str, *, allow_disabled: bool = False) -> SkillMetadata:
        try:
            skill = self.skills[skill_name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {skill_name}") from exc
        if not allow_disabled and not skill.enabled:
            raise ValueError(f"skill '{skill_name}' is disabled")
        return skill
