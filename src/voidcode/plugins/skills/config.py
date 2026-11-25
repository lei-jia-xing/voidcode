"""Strict plugin configuration for workspace skills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_SKILL_SEARCH_PATHS: tuple[str, ...] = (".voidcode/skills",)


@dataclass(frozen=True, slots=True)
class SkillEnableRule:
    """One ``config`` entry: enable/disable a skill by exact name or by file/directory path."""

    name: str | None = None
    path: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        has_name = self.name is not None
        has_path = self.path is not None
        if has_name == has_path:
            raise ValueError("skill config rule requires exactly one of 'name' or 'path'")
        if has_name:
            if not isinstance(self.name, str) or not self.name.strip():
                raise ValueError("skill config rule 'name' must be a non-empty string")
            object.__setattr__(self, "name", self.name.strip())
        if has_path:
            if not isinstance(self.path, str) or not self.path.strip():
                raise ValueError("skill config rule 'path' must be a non-empty string")
            object.__setattr__(self, "path", self.path.strip())
        object.__setattr__(self, "enabled", bool(self.enabled))


@dataclass(frozen=True, slots=True)
class SkillsConfig:
    enabled: bool = False
    paths: tuple[str, ...] = DEFAULT_SKILL_SEARCH_PATHS
    rules: tuple[SkillEnableRule, ...] = ()


def _config_bool(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"skills config field '{field_name}' must be a boolean")
    return value


def parse_skills_config(config: Mapping[str, object] | None) -> SkillsConfig:
    if config is None:
        return SkillsConfig()
    unknown = sorted(str(key) for key in config if key not in {"enabled", "paths", "config", "providers"})
    if unknown:
        raise ValueError(f"skills config field '{unknown[0]}' is not supported")
    enabled = _config_bool(config.get("enabled"), field_name="enabled", default=False)

    raw_paths = config.get("paths")
    if raw_paths is None:
        paths = DEFAULT_SKILL_SEARCH_PATHS
    else:
        if not isinstance(raw_paths, (list, tuple)):
            raise ValueError("skills config field 'paths' must be an array of strings")
        validated_paths: list[str] = []
        for item in raw_paths:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("skills config field 'paths' must contain non-empty strings")
            validated_paths.append(item)
        paths = tuple(validated_paths)

    raw_rules = config.get("config")
    rules: list[SkillEnableRule] = []
    if raw_rules is not None:
        if not isinstance(raw_rules, (list, tuple)):
            raise ValueError("skills config field 'config' must be an array of objects")
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, Mapping):
                raise ValueError("skills config rules must be objects")
            unknown_rule = sorted(str(key) for key in raw_rule if key not in {"name", "path", "enabled"})
            if unknown_rule:
                raise ValueError(f"skills config rule field '{unknown_rule[0]}' is not supported")
            name = raw_rule.get("name")
            path = raw_rule.get("path")
            if (name is None) == (path is None):
                raise ValueError("skills config rule requires exactly one of 'name' or 'path'")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError("skills config rule 'name' must be a non-empty string")
            if path is not None and (not isinstance(path, str) or not path.strip()):
                raise ValueError("skills config rule 'path' must be a non-empty string")
            try:
                entity = SkillEnableRule(
                    name=name.strip() if isinstance(name, str) else None,
                    path=path.strip() if isinstance(path, str) else None,
                    enabled=_config_bool(raw_rule.get("enabled"), field_name="enabled", default=True),
                )
            except ValueError as exc:
                raise ValueError(f"skills config rule: {exc}") from exc
            rules.append(entity)

    return SkillsConfig(enabled=enabled, paths=paths, rules=tuple(rules))
