"""Workspace skill discovery, metadata catalog, on-demand loading and rule context.

The registry is rebuilt per call so new SKILL.md files become visible immediately.
Skills require explicit configuration; bodies enter context through the skill:// reader.
AGENTS.md hierarchy and .voidcode/rules guidance are independent of that setting.
Touched paths come from durable public resource facts.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from voidcode.capabilities.frontmatter import load_frontmatter_mapping, split_frontmatter
from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES, format_resource_text
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import ContextContribution, InvocationFinishedFact, PluginFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.resources import ResourcePage, ResourceReadCaller, ResourceReaderServices, ResourceReadRequest
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import OutputBounds, ToolEffect
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

from .rules import build_rule_catalog, render_file_rules, rulebook_prompt_context, runtime_file_rule_contexts

SKILL_ENTRY_FILE_NAME = "SKILL.md"
DEFAULT_SKILL_SEARCH_PATHS: tuple[str, ...] = (".voidcode/skills",)
SUPPORTED_FRONTMATTER_KEYS: frozenset[str] = frozenset({"name", "description"})

SKILL_URI_PREFIX = "skill://"
DEFAULT_SKILL_READ_LIMIT = 2000


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


# --- Manifest parsing ---


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
        raw_frontmatter, body = split_frontmatter(contents)
        parsed = _parse_skill_frontmatter_fields(load_frontmatter_mapping(raw_frontmatter))
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


# --- Discovery ---


class SkillLoadError(ValueError):
    """A discoverable skill file exists but cannot be loaded."""


def resolve_workspace_relative_path(*, workspace: Path, configured_path: str) -> Path:
    candidate_path = Path(configured_path)
    if candidate_path.is_absolute():
        return candidate_path.resolve()

    resolved_path = (workspace / candidate_path).resolve()
    if not resolved_path.is_relative_to(workspace):
        raise ValueError(f"skill search path escapes workspace: {configured_path}")
    return resolved_path


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
            skill_root = resolve_workspace_relative_path(workspace=resolved_workspace, configured_path=search_path)
            if not skill_root.exists() or not skill_root.is_dir():
                continue
            for entry_path in sorted(skill_root.glob(f"**/{SKILL_ENTRY_FILE_NAME}")):
                if not entry_path.is_file():
                    continue
                discovered.append(self.load(entry_path))

        return tuple(discovered)

    def load(self, entry_path: Path) -> SkillMetadata:
        resolved_entry_path = entry_path.resolve()
        try:
            contents = resolved_entry_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillLoadError(f"failed to read skill file {resolved_entry_path}: {exc}") from exc
        try:
            return parse_skill_manifest(contents, entry_path=resolved_entry_path)
        except SkillManifestParseError as exc:
            raise SkillLoadError(str(exc)) from exc


# --- Registry ---


@dataclass(frozen=True, slots=True)
class SkillEnableRule:
    """One ``config`` entry: enable/disable a skill by exact name or by file/directory path."""

    name: str | None = None
    path: str | None = None
    enabled: bool = True


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

    def apply_config_rules(self, rules: Iterable[SkillEnableRule]) -> None:
        for rule in rules:
            if rule.name and rule.name in self.skills:
                self.skills[rule.name] = replace(self.skills[rule.name], enabled=rule.enabled)
            if rule.path:
                try:
                    target_path = Path(rule.path).resolve()
                except OSError:
                    target_path = Path(rule.path)
                for skill_name, skill in list(self.skills.items()):
                    if skill.entry_path.resolve() == target_path or skill.directory.resolve() == target_path:
                        self.skills[skill_name] = replace(skill, enabled=rule.enabled)

    def resolve(self, skill_name: str, *, allow_disabled: bool = False) -> SkillMetadata:
        try:
            skill = self.skills[skill_name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {skill_name}") from exc
        if not allow_disabled and not skill.enabled:
            raise ValueError(f"skill '{skill_name}' is disabled")
        return skill


# --- Plugin configuration (mirrors the legacy skills config section) ---


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
            if name is not None and (not isinstance(name, str) or not name.strip()):
                raise ValueError("skills config rule 'name' must be a non-empty string")
            if path is not None and (not isinstance(path, str) or not path.strip()):
                raise ValueError("skills config rule 'path' must be a non-empty string")
            rules.append(
                SkillEnableRule(
                    name=name.strip() if isinstance(name, str) else None,
                    path=path.strip() if isinstance(path, str) else None,
                    enabled=_config_bool(raw_rule.get("enabled"), field_name="enabled", default=True),
                )
            )

    return SkillsConfig(enabled=enabled, paths=paths, rules=tuple(rules))


def build_registry(*, workspace: Path, config: SkillsConfig) -> SkillRegistry:
    """Rebuild the registry for this call, exactly like the legacy runtime (no cache)."""
    if not config.enabled:
        return SkillRegistry.from_skills(())
    registry = SkillRegistry.discover(workspace=workspace, search_paths=config.paths)
    if config.rules:
        registry.apply_config_rules(config.rules)
    return registry


# --- Runtime contexts and prompt texts ---


def _skill_entry_uri(path: Path) -> str:
    if path.is_absolute():
        return path.as_uri()
    return path.resolve().as_uri()


def loaded_skill_names(skill_registry: SkillRegistry) -> tuple[str, ...]:
    return tuple(sorted(skill_name for skill_name, skill in skill_registry.skills.items() if skill.enabled))


def catalog_skill_context(skill_registry: SkillRegistry) -> str:
    names = loaded_skill_names(skill_registry)
    if not names:
        return ""
    lines = [
        "Runtime skills catalog (recommended/visible).",
        "Each entry is metadata only; the full SKILL.md body is NOT in your",
        "current context.",
        "Load a skill body ONLY when its description matches the active task.",
        'Use the read tool: read(path="skill://..."). Do NOT speculatively load every',
        "skill; lazy loading keeps the harness lightweight and the model focused.",
        "",
        "<available_skills>",
    ]
    for skill_name in names:
        skill = skill_registry.skills[skill_name]
        lines.extend(
            (
                "  <skill>",
                f"    <name>{html.escape(skill.name)}</name>",
                f"    <description>{html.escape(skill.description)}</description>",
                f"    <location>{html.escape(_skill_entry_uri(skill.entry_path))}</location>",
                "  </skill>",
            )
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


# --- Reader ---


def _skill_rendered_content(skill: SkillMetadata) -> str:
    return (
        f"## Skill: {skill.name}\n"
        f"**Description**: {skill.description}\n"
        f"sourcePath: {skill.entry_path}\n"
        f"baseDirectory: {skill.directory}\n\n"
        f"{skill.content}"
    )


def _skill_page(rendered: str, *, uri: str, skill: SkillMetadata, offset: int, limit: int) -> ResourcePage:
    lines = rendered.split("\n")
    total = len(lines)
    if offset > total:
        raise ValueError(f"Offset {offset} is out of range for this skill ({total} lines)")
    start = offset - 1
    shown = lines[start : start + limit]
    next_offset = offset + len(shown) if start + len(shown) < total else None
    truncated = next_offset is not None
    raw_content = "\n".join(shown)
    if shown and (start + len(shown) < total or rendered.endswith("\n")):
        raw_content += "\n"
    summary = f"Read {len(shown)} line(s) from {uri}"
    summary += f"; continue with offset={next_offset}." if next_offset is not None else "."
    return ResourcePage(
        output=format_resource_text(
            summary,
            raw_content=raw_content,
            next_offset=next_offset,
            truncated=truncated,
            partial=truncated,
            bounds=OutputBounds(truncated=truncated, partial=truncated),
        ),
        body={
            "path": uri,
            "type": "skill",
            "name": skill.name,
            "description": skill.description,
            "sourcePath": str(skill.entry_path),
            "baseDirectory": str(skill.directory),
            "offset": offset,
            "offset_unit": "lines",
            "limit": limit,
            "next_offset": next_offset,
            "truncated": truncated,
            "partial": truncated,
            "byte_count": len(raw_content.encode("utf-8")),
            "raw_content": raw_content,
            "line_count": len(shown),
        },
    )


class SkillReader:
    """The ``skill://`` namespace: one discovered skill's rendered SKILL.md per URI."""

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def read(self, request: ResourceReadRequest, caller: ResourceReadCaller, services: ResourceReaderServices) -> ResourcePage | None:
        _ = services
        if request.byte_offset is not None or request.byte_limit is not None:
            raise ValueError("byte_offset and byte_limit are not supported for skill:// URIs")
        name = request.uri[len(SKILL_URI_PREFIX) :].strip()
        if not name:
            raise ValueError("skill://<name> requires a skill name")
        registry = build_registry(workspace=caller.workspace, config=self._config)
        skill = registry.skills.get(name)
        if skill is None:
            return None
        if not skill.enabled:
            raise ValueError(f"skill '{name}' is disabled")
        rendered = _skill_rendered_content(skill)
        if len(rendered.encode("utf-8")) > MAX_RESOURCE_PAGE_BYTES:
            line_count = rendered.count("\n") + (0 if rendered.endswith("\n") else 1)
            raise ValueError(f"skill '{name}' renders {line_count} line(s); narrow the read with offset/limit")
        limit = request.limit if request.limit is not None else DEFAULT_SKILL_READ_LIMIT
        return _skill_page(rendered, uri=request.uri, skill=skill, offset=request.offset, limit=limit)


# --- Command ---


class SkillsCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="skills",
        description="List discovered workspace skills and whether each is enabled.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        config = self._config
        if not config.enabled:
            return CommandResult(
                status="ok",
                output="Skills are disabled.",
                data={"enabled": False, "skills": []},
                presentation=_skills_notice("Skills are disabled. Set plugins.skills.config.enabled to true in voidcode.json and run /core reload."),
            )
        workspace = invocation.context.require_workspace()
        try:
            registry = build_registry(workspace=workspace, config=config)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))

        skills = registry.all()
        rows = [
            {
                "name": skill.name,
                "description": skill.description,
                "enabled": skill.enabled,
                "entry_path": str(skill.entry_path),
                "directory": str(skill.directory),
            }
            for skill in sorted(skills, key=lambda item: item.name)
        ]
        if not rows:
            output = "No skills discovered."
        else:
            lines = []
            for row in rows:
                state = "enabled" if row["enabled"] else "disabled"
                lines.append(f"- [{state}] {row['name']} — {row['description']} ({row['entry_path']})")
            output = "\n".join(lines)
        options = [
            {"id": row["name"], "label": f"{row['name']} — {'enabled' if row['enabled'] else 'disabled'} · {str(row['description'])[:200]}"}
            for row in rows[:100]
        ]
        presentation = (
            CommandPresentation(
                title=f"Skills ({min(len(rows), 100)} of {len(rows)})",
                view_type="choices",
                data={"options": options, "action": "skills.show", "argument_name": "name"},
            )
            if options
            else _skills_notice("No skills discovered. Add a SKILL.md under .voidcode/skills/<name>/, then run /skills again.")
        )
        return CommandResult(status="ok", output=output, data={"enabled": True, "skills": rows}, presentation=presentation)


def _skills_notice(message: str) -> CommandPresentation:
    return CommandPresentation(title="Skills", view_type="notice", data={"message": message})


class SkillShowCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="skills.show",
        description="Inspect a skill's source, enabled state and paged instructions.",
        text_argument="name",
        effects=frozenset({ToolEffect.READ}),
        argument_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "offset": {"type": "integer", "minimum": 0, "description": "Character offset from a previous page."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 6000},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = services
        if not self._config.enabled:
            return _skills_notice("Skills are disabled. Enable plugins.skills.config.enabled in voidcode.json and run /core reload.")
        skills = sorted(build_registry(workspace=invocation.context.require_workspace(), config=self._config).all(), key=lambda skill: skill.name)
        if not skills:
            return _skills_notice("No skills discovered. Add a SKILL.md under .voidcode/skills/<name>/ and retry.")
        return CommandPresentation(
            title="Inspect skill",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "name",
                        "label": f"Skill ({min(len(skills), 100)} of {len(skills)})",
                        "options": [{"id": skill.name, "label": f"{skill.name} — {skill.description[:200]}"} for skill in skills[:100]],
                    }
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        if not self._config.enabled:
            return CommandResult(status="error", error="Skills are disabled. Enable skills in voidcode.json and reload.")
        name = invocation.arguments.get("name")
        offset, limit = invocation.arguments.get("offset", 0), invocation.arguments.get("limit", 4000)
        if not isinstance(name, str) or not name.strip():
            return CommandResult(status="error", error="skills.show requires a skill name")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000:
            return CommandResult(status="error", error="offset must be non-negative and limit must be between 1 and 6000")
        try:
            skill = build_registry(workspace=invocation.context.require_workspace(), config=self._config).skills.get(name.strip())
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))
        if skill is None:
            return CommandResult(status="error", error=f"Unknown skill: {name}")
        if offset > len(skill.content):
            return CommandResult(status="error", error="Skill offset is beyond the end of its instructions")
        end = min(offset + limit, len(skill.content))
        while True:
            content = (
                f"# {skill.name[:256]}\n\n{skill.description[:800]}\n\n"
                f"Status: {'enabled' if skill.enabled else 'disabled'}\n\nSource: `{str(skill.entry_path)[:1500]}`\n\n"
                f"Instructions (characters {offset}–{end} of {len(skill.content)}):\n\n{skill.content[offset:end]}"
            )
            actions: list[dict[str, object]] = []
            if offset:
                actions.append(
                    {
                        "label": "Previous page",
                        "action": "skills.show",
                        "arguments": {"name": skill.name, "offset": max(0, offset - limit), "limit": limit},
                    }
                )
            if end < len(skill.content):
                actions.append({"label": "Next page", "action": "skills.show", "arguments": {"name": skill.name, "offset": end, "limit": limit}})
            actions.append({"label": "Back to skills", "action": "skills"})
            if len(json.dumps({"content": content, "actions": actions}).encode("utf-8")) <= MAX_VIEW_DATA_BYTES - 4096:
                break
            if end <= offset + 1:
                return CommandResult(status="error", error="Skill metadata exceeds the detail view limit; shorten its name or description")
            end = offset + max(1, (end - offset) // 2)
        return CommandResult(
            status="ok",
            output=content,
            data={
                "name": skill.name,
                "enabled": skill.enabled,
                "offset": offset,
                "offset_unit": "characters",
                "next_offset": end if end < len(skill.content) else None,
                "total_characters": len(skill.content),
            },
            presentation=CommandPresentation(title=f"Skill: {skill.name[:256]}", view_type="markdown", data={"content": content, "actions": actions}),
        )


# --- Touched-path session state (feeds the file-rule hierarchy) ---

TOUCHED_PATHS_LIMIT = 64
RULES_STATE_SCHEMA_VERSION = 1
_RULES_STATE_KEYS: frozenset[str] = frozenset({"schema_version", "cursor_sequence", "touched_paths"})
_EVENT_PAGE_SIZE = 200
_MAX_EVENT_PAGES = 8


@dataclass(frozen=True, slots=True)
class RulesState:
    """Session-scope record of the workspace paths this session's tools touched.

    ``cursor_sequence`` is the last committed session event this owner consumed, so the
    public resource facts that feed ``touched_paths`` stay forward-only and bounded.
    """

    touched_paths: tuple[str, ...] = ()
    cursor_sequence: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, object] | None) -> RulesState:
        if payload is None:
            return cls()
        unknown = sorted(str(key) for key in payload if key not in _RULES_STATE_KEYS)
        if unknown:
            raise ValueError(f"skills state field '{unknown[0]}' is not supported")
        missing = _RULES_STATE_KEYS - payload.keys()
        if missing:
            raise ValueError("skills state is missing field(s): " + ", ".join(sorted(missing)))
        if type(payload["schema_version"]) is not int or payload["schema_version"] != RULES_STATE_SCHEMA_VERSION:
            raise ValueError(f"skills state schema_version must be {RULES_STATE_SCHEMA_VERSION}")
        cursor = payload["cursor_sequence"]
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("skills state cursor_sequence must be a non-negative integer")
        raw_paths = payload["touched_paths"]
        if not isinstance(raw_paths, (list, tuple)):
            raise ValueError("skills state touched_paths must be an array of strings")
        paths: list[str] = []
        for item in raw_paths:
            if not isinstance(item, str) or not item:
                raise ValueError("skills state touched_paths must contain non-empty strings")
            paths.append(item)
        return cls(touched_paths=tuple(paths), cursor_sequence=cursor)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": RULES_STATE_SCHEMA_VERSION,
            "cursor_sequence": self.cursor_sequence,
            "touched_paths": list(self.touched_paths),
        }


def _workspace_relative_path(raw_path: str, *, workspace_root: Path) -> str | None:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(workspace_root)
    except ValueError:
        return None
    return relative.as_posix()


def _record_touched_paths(state: RulesState, *, workspace_root: Path, raw_paths: Iterable[str]) -> RulesState:
    """Deduplicate into recency order, keep the most recent ``TOUCHED_PATHS_LIMIT`` entries."""
    touched = list(state.touched_paths)
    changed = False
    for raw_path in raw_paths:
        relative = _workspace_relative_path(raw_path, workspace_root=workspace_root)
        if relative is None:
            continue
        touched = [existing for existing in touched if existing != relative]
        touched.append(relative)
        changed = True
    if not changed:
        return state
    return replace(state, touched_paths=tuple(touched[-TOUCHED_PATHS_LIMIT:]))


def _load_rules_state(services: PluginServices) -> tuple[RulesState, int]:
    payload, version = services.get_state("session")
    return RulesState.from_payload(payload), version


def _consume_resource_facts(state: RulesState, services: PluginServices) -> tuple[int, tuple[str, ...]]:
    """Read committed resource observations without depending on event or tool names."""
    cursor = state.cursor_sequence
    found: list[str] = []
    for _ in range(_MAX_EVENT_PAGES):
        page = services.read_facts(after_sequence=cursor, limit=_EVENT_PAGE_SIZE)
        for fact in page.facts:
            if isinstance(fact, InvocationFinishedFact):
                found.extend(fact.resource_paths)
        cursor = page.next_sequence
        if not page.has_more:
            break
    return cursor, tuple(found)


def skills_observer(fact: PluginFact, context: ToolContext, services: PluginServices) -> None:
    """Record the workspace paths each finished invocation touched, for the next dispatch."""
    if not isinstance(fact, InvocationFinishedFact):
        return
    state, version = _load_rules_state(services)
    cursor, raw_paths = _consume_resource_facts(state, services)
    updated = _record_touched_paths(
        replace(state, cursor_sequence=cursor),
        workspace_root=context.workspace.resolve(strict=False),
        raw_paths=raw_paths,
    )
    if updated != state:
        services.replace_state(updated.to_payload(), expected_version=version, scope="session")


# --- Context ---


class SkillsContextProvider:
    """Per-dispatch contribution: skill catalog, file rules, then the rulebook.

    Rule context is independent of the skills config, exactly like the legacy runtime: the
    file-rule transform shipped the workspace ``AGENTS.md`` hierarchy and the rulebook whether
    or not skills were enabled. The skills catalog stays gated on ``enabled``.
    """

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def __call__(self, context: ToolContext, services: PluginServices) -> ContextContribution | None:
        config = self._config
        workspace = context.require_workspace()
        parts: list[str] = []
        if config.enabled:
            registry = build_registry(workspace=workspace, config=config)
            catalog = catalog_skill_context(registry)
            if catalog:
                parts.append(catalog)
        state, _version = _load_rules_state(services)
        file_rules = render_file_rules(runtime_file_rule_contexts(workspace=workspace, touched_paths=state.touched_paths))
        if file_rules:
            parts.append(file_rules)
        rulebook_catalog = build_rule_catalog(workspace)
        if rulebook_catalog.entries:
            rulebook = rulebook_prompt_context(rulebook_catalog)
            if rulebook:
                parts.append(rulebook)
        return None if not parts else ContextContribution(content="\n\n".join(parts))


def prepare_skills_deactivation(
    state: Mapping[str, object],
    scope: str,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: the observation cursor and recency list survive untouched.

    Both are plain history (paths, not leases) with no live resource behind them, so the
    parked copy keeps the payload as-is and re-enable resumes exactly where it left off.
    """
    _ = (scope, scope_key, tip_sequence)
    return state
