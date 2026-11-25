"""Metadata-only catalog text and bounded diagnostics for provider context."""

from __future__ import annotations

import html
from pathlib import Path

from .limits import (
    MAX_SKILL_CATALOG_BYTES,
    MAX_SKILL_DIAGNOSTIC_BYTES,
    MAX_SKILL_DIAGNOSTICS,
    truncate_utf8,
)
from .manifest import SKILL_URI_PREFIX, SkillMetadata
from .registry import SkillRegistry


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
        lines.append(f"- {failure.display_name} ({failure.path}): {failure.message}")
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
    if read_tool_available is False:
        load_lines = ("Skill bodies cannot be loaded in this session: the read tool is unavailable.",)
    else:
        load_lines = (
            "Load a skill body ONLY when its description matches the active task.",
            'Use the read tool: read(path="skill://<name>"). Do NOT speculatively load every',
            "skill; lazy loading keeps the harness lightweight and the model focused.",
        )
    header = "\n".join(
        (
            "Runtime skills catalog (recommended/visible).",
            "Each entry is metadata only; the full SKILL.md body is NOT in your",
            "current context.",
            *load_lines,
            "",
            "<available_skills>",
        )
    )
    closing = "</available_skills>"
    suffix = ""
    if diagnostics:
        block = "<skill_diagnostics>\n" + "\n".join(diagnostics) + "\n</skill_diagnostics>"
        suffix += "\n" + truncate_utf8(block, limit=MAX_SKILL_DIAGNOSTIC_BYTES, marker="\n[... diagnostics truncated]")

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
    return truncate_utf8(document, limit=MAX_SKILL_CATALOG_BYTES, marker="\n[... skills catalog truncated]")
