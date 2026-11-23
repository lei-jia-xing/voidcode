"""Workspace skill discovery, metadata catalog, on-demand loading and rule context.

The registry is rebuilt per call so new SKILL.md files become visible immediately.
Skills require explicit configuration; bodies enter context through the skill:// reader.
AGENTS.md hierarchy and .voidcode/rules guidance are independent of that setting.
Touched paths come from durable public resource facts.
"""

from __future__ import annotations

import codecs
import html
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from voidcode.capabilities.frontmatter import load_frontmatter_mapping, split_frontmatter
from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES, format_resource_text, raise_if_cancelled
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import ContextContribution, InvocationFinishedFact, PluginFact
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import StateScope
from voidcode.sdk.resources import ResourcePage, ResourceReadCaller, ResourceReaderServices, ResourceReadRequest
from voidcode.sdk.services import HistoryServices, PluginServices, StateServices
from voidcode.sdk.tools import OutputBounds, ToolEffect
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

from .rules import (
    build_rule_catalog,
    render_file_rules,
    render_rule_diagnostics,
    rulebook_prompt_context,
    runtime_file_rule_result,
)

SKILL_ENTRY_FILE_NAME = "SKILL.md"
DEFAULT_SKILL_SEARCH_PATHS: tuple[str, ...] = (".voidcode/skills",)
SUPPORTED_FRONTMATTER_KEYS: frozenset[str] = frozenset({"name", "description"})

SKILL_URI_PREFIX = "skill://"
DEFAULT_SKILL_READ_LIMIT = 2000

#: Discovery and ingestion bounds. These keep one workspace from turning every provider
#: dispatch into an unbounded read, and they are reported per entry rather than silently
#: dropped. Search roots themselves stay configured (absolute roots are trusted as given).
MAX_SKILLS = 64
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_NAME_CHARS = 128
MAX_SKILL_DESCRIPTION_CHARS = 1024
MAX_SKILL_BODY_BYTES = 48 * 1024
MAX_SKILL_CATALOG_BYTES = 8 * 1024
MAX_SKILL_CONTEXT_BYTES = 64 * 1024
MAX_SKILL_DIAGNOSTICS = 32
MAX_SKILL_DIAGNOSTIC_CHARS = 300
MAX_SKILL_DIAGNOSTIC_BYTES = 2 * 1024


def _bounded_message(message: object, *, limit: int = MAX_SKILL_DIAGNOSTIC_CHARS) -> str:
    """One single-line diagnostic bounded by characters, visibly marked when cut."""
    compact = " ".join(str(message).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _truncate_utf8(text: str, *, limit: int, marker: str) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes and append a visible marker."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= limit:
        return marker_bytes[:limit].decode("utf-8", errors="ignore")
    return encoded[: limit - len(marker_bytes)].decode("utf-8", errors="ignore") + marker


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
        if len(self.name) > MAX_SKILL_NAME_CHARS:
            raise ValueError(f"skill name must not exceed {MAX_SKILL_NAME_CHARS} characters (got {len(self.name)})")
        if len(self.description) > MAX_SKILL_DESCRIPTION_CHARS:
            raise ValueError(f"skill description must not exceed {MAX_SKILL_DESCRIPTION_CHARS} characters (got {len(self.description)})")
        if len(self.content.encode("utf-8")) > MAX_SKILL_BODY_BYTES:
            raise ValueError(f"skill body must not exceed {MAX_SKILL_BODY_BYTES} bytes")


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
        parsed[key] = _validated_non_empty_string(value, field_name=f"skill frontmatter field '{key}'")

    missing_keys = SUPPORTED_FRONTMATTER_KEYS.difference(parsed)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise SkillManifestParseError(f"skill frontmatter missing required fields: {missing}")
    return parsed


def parse_skill_manifest(contents: str, *, entry_path: Path) -> SkillMetadata:
    try:
        raw_frontmatter, body = split_frontmatter(contents)
        parsed = _parse_skill_frontmatter_fields(load_frontmatter_mapping(raw_frontmatter))
        # split_frontmatter strips surrounding whitespace; content is that stripped body.
        _validated_non_empty_string(body, field_name="content")
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
    """A discoverable skill file exists but cannot be loaded.

    ``name_hint`` lets the registry expose a stable ``skill://`` key (and a path-based
    config rule) for an entry whose own metadata is unusable.
    """

    def __init__(self, message: str, *, name_hint: str | None = None, path: Path | None = None) -> None:
        super().__init__(message)
        self.name_hint = name_hint
        self.path = path


@dataclass(frozen=True, slots=True)
class SkillLoadFailure:
    """One discovered entry that could not be loaded; never aborts healthy siblings."""

    name: str
    path: Path
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _bounded_message(self.name, limit=MAX_SKILL_NAME_CHARS))
        object.__setattr__(self, "message", _bounded_message(self.message))


@dataclass(frozen=True, slots=True)
class SkillDiscovery:
    """Result of scanning configured roots: healthy skills, per-entry failures, config errors."""

    skills: tuple[SkillMetadata, ...] = ()
    failures: tuple[SkillLoadFailure, ...] = ()
    config_errors: tuple[str, ...] = ()


def resolve_workspace_relative_path(*, workspace: Path, configured_path: str) -> Path:
    candidate_path = Path(configured_path)
    if candidate_path.is_absolute():
        return candidate_path.resolve()

    resolved_path = (workspace / candidate_path).resolve()
    if not resolved_path.is_relative_to(workspace):
        raise ValueError(f"skill search path escapes workspace: {configured_path}")
    return resolved_path


def _best_effort_skill_name(contents: str) -> str | None:
    """The declared name when frontmatter happens to be parseable; used only for diagnostics."""
    try:
        raw_frontmatter, _body = split_frontmatter(contents)
        payload = load_frontmatter_mapping(raw_frontmatter)
    except ValueError:
        return None
    name = payload.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


class LocalSkillMetadataLoader:
    def discover(
        self,
        *,
        workspace: Path,
        search_paths: Iterable[str] = DEFAULT_SKILL_SEARCH_PATHS,
    ) -> SkillDiscovery:
        resolved_workspace = workspace.resolve()
        discovered: list[SkillMetadata] = []
        failures: list[SkillLoadFailure] = []
        config_errors: list[str] = []
        seen_entries: set[Path] = set()
        hidden_failures = 0
        stopped = False

        def record_failure(failure: SkillLoadFailure) -> None:
            nonlocal hidden_failures
            if len(failures) < MAX_SKILL_DIAGNOSTICS:
                failures.append(failure)
            else:
                hidden_failures += 1

        for search_path in search_paths:
            try:
                relative_root = not Path(search_path).expanduser().is_absolute()
                skill_root = resolve_workspace_relative_path(workspace=resolved_workspace, configured_path=search_path)
            except (OSError, ValueError) as exc:
                config_errors.append(_bounded_message(f"search path '{search_path}': {exc}; ignored"))
                continue
            if not skill_root.is_dir():
                continue
            try:
                entries = sorted(skill_root.glob(f"**/{SKILL_ENTRY_FILE_NAME}"))
            except OSError as exc:
                config_errors.append(_bounded_message(f"search path '{search_path}': {exc}; ignored"))
                continue
            for entry_path in entries:
                resolved_entry = entry_path.resolve(strict=False)
                # Overlapping roots commonly reach the same file; load it once.
                if resolved_entry in seen_entries:
                    continue
                if relative_root and not resolved_entry.is_relative_to(resolved_workspace):
                    seen_entries.add(resolved_entry)
                    record_failure(
                        SkillLoadFailure(
                            name=entry_path.parent.name or SKILL_ENTRY_FILE_NAME,
                            path=entry_path,
                            message="entry resolves outside the workspace; ignored",
                        )
                    )
                    continue
                if not resolved_entry.is_file():
                    continue
                seen_entries.add(resolved_entry)
                if len(discovered) >= MAX_SKILLS:
                    if len(failures) >= MAX_SKILL_DIAGNOSTICS:
                        config_errors.append(
                            _bounded_message(f"skill discovery cap of {MAX_SKILLS} skills reached; further entries under {skill_root} were ignored")
                        )
                        stopped = True
                        break
                    record_failure(
                        SkillLoadFailure(
                            name=entry_path.parent.name or SKILL_ENTRY_FILE_NAME,
                            path=entry_path,
                            message=f"not loaded: discovery cap of {MAX_SKILLS} skills reached",
                        )
                    )
                    continue
                try:
                    discovered.append(self.load(resolved_entry))
                except (SkillLoadError, OSError) as exc:
                    hint = getattr(exc, "name_hint", None) or entry_path.parent.name or SKILL_ENTRY_FILE_NAME
                    record_failure(SkillLoadFailure(name=hint, path=resolved_entry, message=str(exc)))
            if stopped:
                break

        if hidden_failures:
            config_errors.append(_bounded_message(f"{hidden_failures} additional broken or over-cap skill entr(ies) were not loaded or reported"))
        return SkillDiscovery(
            skills=tuple(discovered),
            failures=tuple(failures),
            config_errors=tuple(config_errors[:MAX_SKILL_DIAGNOSTICS]),
        )

    def load(self, entry_path: Path) -> SkillMetadata:
        resolved_entry_path = entry_path.resolve()
        try:
            with resolved_entry_path.open("rb") as handle:
                data = handle.read(MAX_SKILL_FILE_BYTES + 1)
        except OSError as exc:
            raise SkillLoadError(
                f"failed to read skill file {resolved_entry_path}: {_bounded_message(exc)}",
                path=resolved_entry_path,
            ) from exc
        if len(data) > MAX_SKILL_FILE_BYTES:
            raise SkillLoadError(
                f"skill file exceeds the {MAX_SKILL_FILE_BYTES}-byte cap; keep SKILL.md below that size",
                path=resolved_entry_path,
            )
        try:
            contents = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLoadError(f"skill file is not valid UTF-8 (invalid byte at offset {exc.start})", path=resolved_entry_path) from exc
        try:
            return parse_skill_manifest(contents, entry_path=resolved_entry_path)
        except SkillManifestParseError as exc:
            name_hint = _best_effort_skill_name(contents)
            raise SkillLoadError(str(exc), name_hint=name_hint, path=resolved_entry_path) from exc


# --- Registry ---


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
        for skill in skills:
            existing = resolved.get(skill.name)
            if existing is not None:
                raise ValueError(f"duplicate skill name '{skill.name}' discovered at {existing.entry_path} and {skill.entry_path}")
            resolved[skill.name] = skill
        return cls(
            skills=resolved,
            failures=failures,
            config_errors=[_bounded_message(message) for message in config_errors],
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
        return next((failure for failure in self.failures if failure.name == skill_name), None)

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
                    self.config_errors.append(_bounded_message(f"config rule path '{rule.path}' is relative but no workspace is known; ignored"))
                    continue
                else:
                    target = _resolved(base / raw_path)
                    if not target.is_relative_to(base):
                        self.config_errors.append(_bounded_message(f"config rule path '{rule.path}' escapes the workspace; ignored"))
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
                    if failure.name != rule.name and (target is None or not _failure_matches_path(failure, target))
                )

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


def _skill_load_target(skill_name: str) -> str:
    return f"{SKILL_URI_PREFIX}{skill_name}"


def _catalog_diagnostics(skill_registry: SkillRegistry) -> tuple[str, ...]:
    lines: list[str] = []
    hidden = 0
    for failure in skill_registry.failures:
        if len(lines) >= MAX_SKILL_DIAGNOSTICS:
            hidden += 1
            continue
        lines.append(f"- {failure.name} ({failure.path}): {failure.message}")
    for error in skill_registry.config_errors:
        if len(lines) >= MAX_SKILL_DIAGNOSTICS:
            hidden += 1
            continue
        lines.append(f"- config: {error}")
    if hidden:
        lines.append(f"- {hidden} more diagnostic(s) omitted")
    return tuple(lines)


def _catalog_skill_entry(skill: SkillMetadata) -> str:
    return "\n".join(
        (
            "  <skill>",
            f"    <name>{html.escape(skill.name)}</name>",
            f"    <description>{html.escape(skill.description)}</description>",
            f"    <location>{html.escape(_skill_load_target(skill.name))}</location>",
            f"    <sourcePath>{html.escape(_skill_entry_uri(skill.entry_path))}</sourcePath>",
            "  </skill>",
        )
    )


def catalog_skill_context(skill_registry: SkillRegistry, *, read_tool_available: bool | None = None) -> str:
    """Metadata catalog plus bounded failure/config diagnostics.

    ``read_tool_available`` is ``None`` when the caller cannot observe the session's
    tools; when explicitly ``False`` the catalog says bodies cannot be loaded instead
    of promising a tool that is not there.
    """
    names = loaded_skill_names(skill_registry)
    diagnostics = _catalog_diagnostics(skill_registry)
    if not names and not diagnostics:
        return ""
    header = "\n".join(
        (
            "Runtime skills catalog (recommended/visible).",
            "Each entry is metadata only; the full SKILL.md body is NOT in your",
            "current context.",
            "Load a skill body ONLY when its description matches the active task.",
            'Use the read tool: read(path="skill://<name>"). Do NOT speculatively load every',
            "skill; lazy loading keeps the harness lightweight and the model focused.",
            "",
            "<available_skills>",
        )
    )
    closing = "</available_skills>"
    suffix = ""
    if diagnostics:
        block = "<skill_diagnostics>\n" + "\n".join(diagnostics) + "\n</skill_diagnostics>"
        suffix += "\n" + _truncate_utf8(block, limit=MAX_SKILL_DIAGNOSTIC_BYTES, marker="\n[... diagnostics truncated]")
    if read_tool_available is False and names:
        note = (
            "Note: the read tool is not available in this session, so skill:// bodies cannot be loaded. "
            "Enable the files plugin (or an equivalent resource reader) before relying on this catalog."
        )
        suffix += "\n" + _truncate_utf8(note, limit=MAX_SKILL_DIAGNOSTIC_BYTES, marker="...")

    budget = MAX_SKILL_CATALOG_BYTES - len(header.encode("utf-8")) - len(closing.encode("utf-8")) - len(suffix.encode("utf-8")) - 2
    entries: list[str] = []
    omitted = 0
    for skill_name in names:
        entry = _catalog_skill_entry(skill_registry.skills[skill_name])
        entry_bytes = len(entry.encode("utf-8")) + (1 if entries else 0)
        if entry_bytes > budget:
            omitted = len(names) - len(entries)
            break
        entries.append(entry)
        budget -= entry_bytes
    if omitted:
        notice = f"  <notice>{omitted} enabled skill(s) omitted: catalog byte cap {MAX_SKILL_CATALOG_BYTES} reached.</notice>"
        notice_bytes = len(notice.encode("utf-8")) + (1 if entries else 0)
        if notice_bytes <= budget:
            entries.append(notice)
        else:
            suffix += f"\n{omitted} enabled skill(s) omitted: catalog byte cap reached."
    body = "\n".join(entries)
    document = f"{header}\n{body}\n{closing}{suffix}" if body else f"{header}\n{closing}{suffix}"
    return _truncate_utf8(document, limit=MAX_SKILL_CATALOG_BYTES, marker="\n[... skills catalog truncated]")


# --- Reader ---


def _skill_rendered_content(skill: SkillMetadata) -> str:
    return (
        f"## Skill: {skill.name}\n"
        f"**Description**: {skill.description}\n"
        f"sourcePath: {skill.entry_path}\n"
        f"baseDirectory: {skill.directory}\n\n"
        f"{skill.content}"
    )


def _skill_byte_page(
    rendered: str,
    *,
    uri: str,
    skill: SkillMetadata,
    byte_offset: int,
    caller: ResourceReadCaller,
    byte_limit: int | None = None,
    reason: str = "",
) -> ResourcePage:
    """One byte-bounded page, mirroring the shared artifact pager for oversized lines."""
    raise_if_cancelled(caller)
    data = rendered.encode("utf-8")
    if byte_offset > len(data):
        raise ValueError(f"byte offset {byte_offset} is out of range for this skill ({len(data)} bytes)")
    limit = min(byte_limit if byte_limit is not None else MAX_RESOURCE_PAGE_BYTES, MAX_RESOURCE_PAGE_BYTES)
    end = min(len(data), byte_offset + limit)
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        raw_content = decoder.decode(data[byte_offset:end], final=end == len(data))
    except UnicodeDecodeError as exc:
        raise ValueError("byte_offset is not a UTF-8 character boundary for this skill") from exc
    consumed = end - byte_offset - len(decoder.getstate()[0])
    if consumed <= 0 and byte_offset < len(data):
        raise ValueError("byte limit is too small to decode one UTF-8 character")
    next_byte_offset = None if end == len(data) else byte_offset + consumed
    truncated = next_byte_offset is not None
    summary = f"Read {consumed} byte(s) from {uri} at byte offset {byte_offset}"
    if reason:
        summary += f" ({reason})"
    summary += f"; continue with byte_offset={next_byte_offset}." if next_byte_offset is not None else "."
    return ResourcePage(
        output=format_resource_text(
            summary,
            raw_content=raw_content,
            next_byte_offset=next_byte_offset,
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
            "byte_offset": byte_offset,
            "offset_unit": "bytes",
            "byte_limit": limit,
            "next_byte_offset": next_byte_offset,
            "truncated": truncated,
            "partial": truncated,
            "byte_count": consumed,
            "raw_content": raw_content,
        },
    )


def _skill_page(
    rendered: str,
    *,
    uri: str,
    skill: SkillMetadata,
    offset: int,
    limit: int,
    caller: ResourceReadCaller,
) -> ResourcePage:
    raise_if_cancelled(caller)
    lines = rendered.split("\n")
    total = len(lines)
    if offset > total:
        raise ValueError(f"Offset {offset} is out of range for this skill ({total} lines)")
    start = offset - 1
    shown = lines[start : start + limit]

    def render_shown() -> str:
        raw_content = "\n".join(shown)
        if shown and (start + len(shown) < total or rendered.endswith("\n")):
            raw_content += "\n"
        return raw_content

    raw_content = render_shown()
    byte_trimmed = False
    while shown and len(raw_content.encode("utf-8")) > MAX_RESOURCE_PAGE_BYTES:
        byte_trimmed = True
        shown.pop()
        raw_content = render_shown()
    if not shown and start < total:
        line_start = len("\n".join(lines[:start]).encode("utf-8")) + (1 if start else 0)
        return _skill_byte_page(
            rendered,
            uri=uri,
            skill=skill,
            byte_offset=line_start,
            caller=caller,
            reason=f"line {offset} exceeds the {MAX_RESOURCE_PAGE_BYTES}-byte page cap",
        )
    next_offset = offset + len(shown) if start + len(shown) < total else None
    truncated = next_offset is not None
    summary = f"Read {len(shown)} line(s) from {uri}"
    if byte_trimmed:
        summary += f" (page byte cap {MAX_RESOURCE_PAGE_BYTES} reached)"
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
            "byte_cap": MAX_RESOURCE_PAGE_BYTES,
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
        raise_if_cancelled(caller)
        name = request.uri[len(SKILL_URI_PREFIX) :].strip()
        if not name:
            raise ValueError("skill://<name> requires a skill name")
        registry = build_registry(workspace=caller.workspace, config=self._config)
        skill = registry.skills.get(name)
        if skill is None:
            failure = registry.failure(name)
            if failure is not None:
                raise ValueError(f"skill '{name}' is unavailable: {failure.message} ({failure.path})")
            return None
        if not skill.enabled:
            raise ValueError(f"skill '{name}' is disabled")
        rendered = _skill_rendered_content(skill)
        if request.byte_offset is not None:
            return _skill_byte_page(
                rendered,
                uri=request.uri,
                skill=skill,
                byte_offset=request.byte_offset,
                byte_limit=request.byte_limit,
                caller=caller,
            )
        limit = min(request.limit if request.limit is not None else DEFAULT_SKILL_READ_LIMIT, DEFAULT_SKILL_READ_LIMIT)
        return _skill_page(rendered, uri=request.uri, skill=skill, offset=request.offset, limit=limit, caller=caller)


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

        rows = [
            {
                "name": skill.name,
                "description": skill.description,
                "enabled": skill.enabled,
                "status": "enabled" if skill.enabled else "disabled",
                "entry_path": str(skill.entry_path),
                "directory": str(skill.directory),
                "error": None,
                "recovery": None,
            }
            for skill in sorted(registry.all(), key=lambda item: item.name)
        ]
        rows.extend(
            {
                "name": failure.name,
                "description": "",
                "enabled": False,
                "status": "broken",
                "entry_path": str(failure.path),
                "directory": str(failure.path.parent),
                "error": failure.message,
                "recovery": _broken_skill_recovery(failure),
            }
            for failure in registry.failures
        )
        rows.sort(key=lambda row: str(row["name"]))
        if not rows:
            output = "No skills discovered."
        else:
            lines = []
            for row in rows:
                if row["status"] == "broken":
                    lines.append(f"- [broken] {row['name']} — {row['error']} ({row['entry_path']}); {row['recovery']}")
                else:
                    lines.append(f"- [{row['status']}] {row['name']} — {row['description']} ({row['entry_path']})")
            if registry.config_errors:
                lines.extend(("", "Configuration diagnostics:"))
                lines.extend(f"- {error}" for error in registry.config_errors)
            output = "\n".join(lines)
        options = [
            {
                "id": str(row["name"]),
                "label": (
                    f"{row['name']} — broken · {str(row['error'])[:160]}"
                    if row["status"] == "broken"
                    else f"{row['name']} — {row['status']} · {str(row['description'])[:200]}"
                ),
            }
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
        return CommandResult(
            status="ok",
            output=output,
            data={"enabled": True, "skills": rows, "diagnostics": list(registry.config_errors)},
            presentation=presentation,
        )


def _broken_skill_recovery(failure: SkillLoadFailure) -> str:
    return f'fix {failure.path} or disable it in plugins.skills.config with {{"path": "<that path>", "enabled": false}}, then run /core reload'


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
        try:
            registry = build_registry(workspace=invocation.context.require_workspace(), config=self._config)
        except (ValueError, OSError) as exc:
            return _skills_notice(f"Skills could not be discovered: {_bounded_message(exc)}")
        skills = sorted(registry.all(), key=lambda skill: skill.name)
        options = [{"id": skill.name, "label": f"{skill.name} — {skill.description[:200]}"} for skill in skills]
        options.extend({"id": failure.name, "label": f"{failure.name} — broken · {failure.message[:200]}"} for failure in registry.failures)
        if not options:
            if registry.config_errors:
                return _skills_notice("Skills could not be discovered: " + "; ".join(registry.config_errors))
            return _skills_notice("No skills discovered. Add a SKILL.md under .voidcode/skills/<name>/ and retry.")
        return CommandPresentation(
            title=f"Inspect skill ({min(len(options), 100)} of {len(options)})",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "name",
                        "label": "Skill",
                        "options": options[:100],
                    }
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        if not self._config.enabled:
            return CommandResult(status="error", error="Skills are disabled. Enable plugins.skills.config.enabled in voidcode.json and reload.")
        name = invocation.arguments.get("name")
        offset, limit = invocation.arguments.get("offset", 0), invocation.arguments.get("limit", 4000)
        if not isinstance(name, str) or not name.strip():
            return CommandResult(status="error", error="skills.show requires a skill name")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000:
            return CommandResult(status="error", error="offset must be non-negative and limit must be between 1 and 6000")
        try:
            registry = build_registry(workspace=invocation.context.require_workspace(), config=self._config)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))
        skill = registry.skills.get(name.strip())
        if skill is None:
            failure = registry.failure(name.strip())
            if failure is not None:
                return CommandResult(
                    status="error",
                    error=f"Skill '{failure.name}' is broken: {failure.message}. {_broken_skill_recovery(failure)}.",
                )
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
            # Exact SDK acceptance: validate_view_data measures this same data mapping
            # with json.dumps(json_wire_object(data), allow_nan=False); no headroom fudge.
            if len(json.dumps(json_wire_object({"content": content, "actions": actions}), allow_nan=False).encode("utf-8")) <= MAX_VIEW_DATA_BYTES:
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
        if len(paths) > TOUCHED_PATHS_LIMIT:
            raise ValueError(f"skills state touched_paths must contain at most {TOUCHED_PATHS_LIMIT} entries")
        if len(paths) != len(set(paths)):
            raise ValueError("skills state touched_paths must not contain duplicates")
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


def _load_rules_state(services: StateServices) -> tuple[RulesState, int]:
    payload, version = services.get_state("session")
    return RulesState.from_payload(payload), version


def _consume_resource_facts(state: RulesState, services: HistoryServices) -> tuple[int, tuple[str, ...]]:
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
            tool_lookup = getattr(services, "tool_definition", None)
            read_tool_available = None if tool_lookup is None else tool_lookup("read") is not None
            catalog = catalog_skill_context(registry, read_tool_available=read_tool_available)
            if catalog:
                parts.append(catalog)
        state, _version = _load_rules_state(services)
        file_rules = runtime_file_rule_result(workspace=workspace, touched_paths=state.touched_paths)
        rendered_file_rules = render_file_rules(file_rules.contexts)
        if rendered_file_rules:
            parts.append(rendered_file_rules)
        rule_diagnostics = render_rule_diagnostics(file_rules.diagnostics)
        if rule_diagnostics:
            parts.append(rule_diagnostics)
        rulebook_catalog = build_rule_catalog(workspace)
        if rulebook_catalog.entries or rulebook_catalog.diagnostics:
            rulebook = rulebook_prompt_context(rulebook_catalog)
            if rulebook:
                parts.append(rulebook)
        if not parts:
            return None
        content = _truncate_utf8(
            "\n\n".join(parts),
            limit=MAX_SKILL_CONTEXT_BYTES,
            marker="\n[skills context truncated by runtime context policy]",
        )
        return ContextContribution(content=content)


def prepare_skills_deactivation(
    state: Mapping[str, object],
    scope: StateScope,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Pure deactivation transform: the observation cursor and recency list survive untouched.

    Both are plain history (paths, not leases) with no live resource behind them, so the
    parked copy keeps the payload as-is and re-enable resumes exactly where it left off.
    """
    _ = (scope, scope_key, tip_sequence)
    return state
