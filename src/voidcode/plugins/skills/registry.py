"""In-memory skill registry and config enable/disable activation rules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import DEFAULT_SKILL_SEARCH_PATHS, SkillEnableRule, SkillsConfig
from .discovery import LocalSkillMetadataLoader, SkillDiscovery, SkillLoadFailure, duplicate_skill_failure
from .limits import bounded_message
from .manifest import SkillMetadata


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def _failure_matches_path(failure: SkillLoadFailure, target: Path) -> bool:
    candidates = {_resolved(failure.path), _resolved(failure.path.parent), failure.path, failure.path.parent}
    return target in candidates


@dataclass(slots=True)
class SkillRegistry:
    skills: dict[str, SkillMetadata] = field(default_factory=dict)
    failures: tuple[SkillLoadFailure, ...] = ()
    config_errors: list[str] = field(default_factory=list)
    workspace: Path | None = None

    @classmethod
    def from_skills(
        cls,
        skills: Iterable[SkillMetadata],
        *,
        failures: tuple[SkillLoadFailure, ...] = (),
        config_errors: Iterable[str] = (),
        workspace: Path | None = None,
    ) -> SkillRegistry:
        resolved: dict[str, SkillMetadata] = {}
        registry_failures = list(failures)
        duplicate_counts: dict[str, int] = {}
        for skill in skills:
            existing = resolved.get(skill.name)
            if existing is not None:
                if _resolved(existing.entry_path) == _resolved(skill.entry_path):
                    continue
                duplicate_counts[skill.name] = duplicate_counts.get(skill.name, 0) + 1
                registry_failures.append(
                    duplicate_skill_failure(
                        name=skill.name,
                        first_path=existing.entry_path,
                        duplicate_path=skill.entry_path,
                        count=duplicate_counts[skill.name],
                    )
                )
                continue
            resolved[skill.name] = skill
        return cls(
            skills=resolved,
            failures=tuple(registry_failures),
            config_errors=[bounded_message(message) for message in config_errors],
            workspace=workspace,
        )

    @classmethod
    def from_discovery(cls, discovery: SkillDiscovery, *, workspace: Path) -> SkillRegistry:
        return cls.from_skills(
            discovery.skills,
            failures=discovery.failures,
            config_errors=discovery.config_errors,
            workspace=workspace,
        )

    @classmethod
    def discover(
        cls,
        *,
        workspace: Path,
        search_paths: Iterable[str] = DEFAULT_SKILL_SEARCH_PATHS,
        loader: LocalSkillMetadataLoader | None = None,
    ) -> SkillRegistry:
        metadata_loader = loader or LocalSkillMetadataLoader()
        resolved_workspace = workspace.resolve()
        discovery = metadata_loader.discover(workspace=resolved_workspace, search_paths=search_paths)
        return cls.from_discovery(discovery, workspace=resolved_workspace)

    def all(self) -> tuple[SkillMetadata, ...]:
        return tuple(self.skills.values())

    def failure(self, skill_name: str) -> SkillLoadFailure | None:
        return next((failure for failure in self.failures if failure.name == skill_name or failure.display_name == skill_name), None)

    def apply_config_rules(self, rules: Iterable[SkillEnableRule], *, workspace: Path | None = None) -> None:
        base = workspace if workspace is not None else self.workspace
        base = _resolved(base) if base is not None else None
        for rule in rules:
            target: Path | None = None
            if rule.path is not None:
                raw_path = Path(rule.path).expanduser()
                if raw_path.is_absolute():
                    target = _resolved(raw_path)
                elif base is None:
                    self.config_errors.append(bounded_message(f"config rule path '{rule.path}' is relative but no workspace is known; ignored"))
                    continue
                else:
                    target = _resolved(base / raw_path)
                    if not target.is_relative_to(base):
                        self.config_errors.append(bounded_message(f"config rule path '{rule.path}' escapes the workspace; ignored"))
                        continue
            if rule.name is not None and rule.name in self.skills:
                self.skills[rule.name] = replace(self.skills[rule.name], enabled=rule.enabled)
            if target is not None:
                for skill_name, skill in list(self.skills.items()):
                    if _resolved(skill.entry_path) == target or _resolved(skill.directory) == target:
                        self.skills[skill_name] = replace(skill, enabled=rule.enabled)
            if not rule.enabled:
                # A disabled malformed entry is quarantined exactly like a healthy one.
                self.failures = tuple(
                    failure
                    for failure in self.failures
                    if failure.name != rule.name
                    and failure.display_name != rule.name
                    and (target is None or not _failure_matches_path(failure, target))
                )

    def resolve(self, skill_name: str, *, allow_disabled: bool = False) -> SkillMetadata:
        try:
            skill = self.skills[skill_name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {skill_name}") from exc
        if not allow_disabled and not skill.enabled:
            raise ValueError(f"skill '{skill_name}' is disabled")
        return skill


def build_registry(*, workspace: Path, config: SkillsConfig) -> SkillRegistry:
    """Rebuild the registry for this call, exactly like the legacy runtime (no cache)."""
    if not config.enabled:
        return SkillRegistry.from_skills(())
    registry = SkillRegistry.discover(workspace=workspace, search_paths=config.paths)
    if config.rules:
        registry.apply_config_rules(config.rules)
    return registry
