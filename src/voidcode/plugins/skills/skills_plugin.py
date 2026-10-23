"""Workspace skill discovery, metadata catalog, on-demand loading and rule context.

The registry is rebuilt per call so new SKILL.md files become visible immediately.
Skills require explicit configuration; bodies enter context through the skill tool.
AGENTS.md hierarchy and .voidcode/rules guidance are independent of that setting.
Touched paths come from durable public resource facts.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from voidcode.capabilities.frontmatter import load_frontmatter_mapping, split_frontmatter
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.hooks import ContextContribution, InvocationFinishedFact, PluginFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin, PluginQueryCaller, StateSchema
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)
from voidcode.sdk.workspace_queries import RULE_PAGES, RulePage, RulePageRequest

from .rules import build_rule_catalog, read_rule_page, render_file_rules, rulebook_prompt_context, runtime_file_rule_contexts

SKILL_ENTRY_FILE_NAME = "SKILL.md"
DEFAULT_SKILL_SEARCH_PATHS: tuple[str, ...] = (".voidcode/skills",)
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
        'Use the skill tool: skill(name="..."). Do NOT speculatively load every',
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


# --- Tool ---


@dataclass(frozen=True, slots=True)
class SkillResultBody:
    name: str
    description: str
    source_path: str
    directory: str
    content: str
    user_message: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "sourcePath": self.source_path,
            "baseDirectory": self.directory,
            "content": self.content,
        }
        if self.user_message is not None:
            payload["user_message"] = self.user_message
        return payload


SKILL_DESCRIPTION = (
    "Load a runtime-discovered skill into the current conversation context.\n"
    "\n"
    "Usage:\n"
    "- Use this tool when the task matches a skill in the runtime skills catalog "
    "listed in the system prompt metadata.\n"
    "- The name argument is required and must match a catalog skill name.\n"
    "- The tool returns the resolved SKILL.md body and metadata so the agent can "
    "follow it in the current turn.\n"
    "- This tool does not create or edit skills; it only loads already-discovered "
    "local skills.\n"
    "- If the skill is unknown, the tool fails instead of guessing."
)


class SkillTool:
    # Static description: the skill catalog lives in system-prompt metadata, not in this tool's
    # description, so the catalog is not shipped twice on every provider request.
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="skill",
        description=SKILL_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name to load."},
                "user_message": {
                    "type": "string",
                    "description": "Optional command arguments or extra context for the skill.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        replay_policy="safe",
    )

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        _ = services

        raw_name = call.arguments.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name:
            message = "skill name must be a non-empty string"
            return ToolFailure(self.definition.name, error=message, output=TextOutput(text=message))
        raw_user_message = call.arguments.get("user_message")
        if raw_user_message is not None and not isinstance(raw_user_message, str):
            message = "skill user_message must be a string"
            return ToolFailure(self.definition.name, error=message, output=TextOutput(text=message))

        try:
            registry = build_registry(workspace=context.require_workspace(), config=self._config)
            skill = registry.resolve(name)
        except (ValueError, OSError) as exc:
            message = str(exc)
            return ToolFailure(self.definition.name, error=message, output=TextOutput(text=message))

        content = (
            f"## Skill: {skill.name}\n"
            f"**Description**: {skill.description}\n"
            f"sourcePath: {skill.entry_path}\n"
            f"baseDirectory: {skill.directory}\n\n"
            f"{skill.content}"
        )
        if raw_user_message:
            content += f"\n\nUser message: {raw_user_message}"
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(content),
            body=SkillResultBody(
                name=skill.name,
                description=skill.description,
                source_path=str(skill.entry_path),
                directory=str(skill.directory),
                content=skill.content,
                user_message=raw_user_message,
            ),
        )


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
            return CommandResult(status="ok", output="Skills are disabled.", data={"enabled": False, "skills": []})
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
        return CommandResult(status="ok", output=output, data={"enabled": True, "skills": rows})


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


def _rule_page_query(request: RulePageRequest, caller: PluginQueryCaller) -> RulePage | None:
    return read_rule_page(caller.workspace, request.uri.strip(), offset=request.offset, limit=request.limit)


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


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    skills_config = parse_skills_config(config)
    return Plugin(
        tools=(SkillTool(skills_config),),
        commands=(SkillsCommand(skills_config),),
        queries=(RULE_PAGES.bind(_rule_page_query),),
        context=SkillsContextProvider(skills_config),
        observer=skills_observer,
        observed_facts=("invocation_finished",),
        state=StateSchema(
            version=1,
            schema={
                "type": "object",
                "required": ["schema_version", "cursor_sequence", "touched_paths"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [1]},
                    "cursor_sequence": {"type": "integer", "minimum": 0},
                    "touched_paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        prepare_deactivate=prepare_skills_deactivation,
    )
