"""Stable facade for workspace skills: discovery, catalog, reader, commands and state.

The implementation is split by concern behind this module so existing imports keep
working:

- :mod:`.manifest` validates ``SKILL.md`` identity and parses frontmatter.
- :mod:`.config` parses the strict plugin configuration.
- :mod:`.discovery` scans roots and isolates per-entry load failures.
- :mod:`.registry` rebuilds the per-call registry and applies enable/disable rules.
- :mod:`.catalog` renders the bounded metadata catalog for provider context.
- :mod:`.reader` serves ``skill://`` with line/byte paging.
- :mod:`.commands` implements ``/skills`` and ``/skills.show``.
- :mod:`.state` owns the touched-path session state and observer.
- :mod:`.context` assembles the catalog, file rules and rulebook contribution.

The registry is rebuilt per call so new SKILL.md files become visible immediately.
Skills require explicit configuration; bodies enter context through the skill:// reader.
AGENTS.md hierarchy and .voidcode/rules guidance are independent of that setting.
Touched paths come from durable public resource facts.
"""

from __future__ import annotations

from .catalog import catalog_skill_context, loaded_skill_names
from .commands import SkillsCommand, SkillShowCommand
from .config import DEFAULT_SKILL_SEARCH_PATHS, SkillEnableRule, SkillsConfig, parse_skills_config
from .context import SkillsContextProvider
from .discovery import LocalSkillMetadataLoader, SkillDiscovery, SkillLoadError, SkillLoadFailure, resolve_workspace_relative_path
from .limits import (
    MAX_SKILL_BODY_BYTES,
    MAX_SKILL_CATALOG_BYTES,
    MAX_SKILL_CONTEXT_BYTES,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_DIAGNOSTIC_BYTES,
    MAX_SKILL_DIAGNOSTIC_CHARS,
    MAX_SKILL_DIAGNOSTICS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_CHARS,
    MAX_SKILLS,
)
from .manifest import (
    SKILL_ENTRY_FILE_NAME,
    SKILL_URI_PREFIX,
    SUPPORTED_FRONTMATTER_KEYS,
    SkillManifestParseError,
    SkillMetadata,
    parse_skill_manifest,
)
from .reader import DEFAULT_SKILL_READ_LIMIT, SkillReader
from .registry import SkillRegistry, build_registry
from .state import (
    RULES_STATE_SCHEMA_VERSION,
    TOUCHED_PATHS_LIMIT,
    RulesState,
    prepare_skills_deactivation,
    skills_observer,
)

__all__ = [
    "DEFAULT_SKILL_READ_LIMIT",
    "DEFAULT_SKILL_SEARCH_PATHS",
    "LocalSkillMetadataLoader",
    "MAX_SKILLS",
    "MAX_SKILL_BODY_BYTES",
    "MAX_SKILL_CATALOG_BYTES",
    "MAX_SKILL_CONTEXT_BYTES",
    "MAX_SKILL_DESCRIPTION_CHARS",
    "MAX_SKILL_DIAGNOSTICS",
    "MAX_SKILL_DIAGNOSTIC_BYTES",
    "MAX_SKILL_DIAGNOSTIC_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_NAME_CHARS",
    "RULES_STATE_SCHEMA_VERSION",
    "RulesState",
    "SKILL_ENTRY_FILE_NAME",
    "SKILL_URI_PREFIX",
    "SUPPORTED_FRONTMATTER_KEYS",
    "SkillDiscovery",
    "SkillEnableRule",
    "SkillLoadError",
    "SkillLoadFailure",
    "SkillManifestParseError",
    "SkillMetadata",
    "SkillReader",
    "SkillRegistry",
    "SkillShowCommand",
    "SkillsCommand",
    "SkillsConfig",
    "SkillsContextProvider",
    "TOUCHED_PATHS_LIMIT",
    "build_registry",
    "catalog_skill_context",
    "loaded_skill_names",
    "parse_skill_manifest",
    "parse_skills_config",
    "prepare_skills_deactivation",
    "resolve_workspace_relative_path",
    "skills_observer",
]
