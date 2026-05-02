from __future__ import annotations

from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path

from .models import SkillMetadata
from .registry import SkillRegistry

_BUILTIN_SKILL_ROOT = Path("/builtin/voidcode/skills")
_BUILTIN_RESOURCE_DIR = "builtin_resources"

# Builtin skill Markdown resources are local package resources. Runtime loading intentionally
# uses importlib.resources, not network URLs.
_GIT_MASTER_DESCRIPTION = (
    "Guidance for safe git workflows, including atomic commits, branch review, "
    "rebase/squash planning, and history searches such as blame, bisect, and "
    "log -S. Use when the user asks about commits, branch state, history, or "
    "auditable repository changes."
)
_FRONTEND_DESIGN_DESCRIPTION = (
    "Guidance for distinctive, production-grade frontend UI/UX work when editing "
    "tools are available. Use when the user asks to build or improve web "
    "components, pages, applications, HTML/CSS layouts, or visual styling while "
    "avoiding generic AI aesthetics."
)
_PLAYWRIGHT_DESCRIPTION = (
    "Guidance for browser-related verification when a configured browser or "
    "Playwright MCP capability is available. Use for navigation checks, form "
    "testing, responsive layout review, screenshots, and end-to-end UI validation "
    "without assuming browser automation is always present."
)
_REVIEW_WORK_DESCRIPTION = (
    "VoidCode-compatible post-implementation review guidance for read-only "
    "verification, code quality, security, QA, and context checks using "
    "supported presets and configured tools only. Triggers: 'review work', "
    "'review my work', 'review changes', 'QA my work', 'verify "
    "implementation', 'check my work', 'validate changes', "
    "'post-implementation review'."
)
_BUILD_VERIFICATION_DESCRIPTION = (
    "Guidance for verifying generated build systems before reporting completion. "
    "Use when creating or modifying CMake, Make, Meson, or similar build "
    "configurations, or when the user asks to build, compile, or verify a "
    "project. Covers configure/build checks, error diagnosis, and evidence "
    "reporting without installing system packages."
)


def _load_builtin_skill_content(resource_filename: str) -> str:
    resource = files(__package__).joinpath(_BUILTIN_RESOURCE_DIR, resource_filename)
    return resource.read_text(encoding="utf-8").strip()


def _builtin_skill(
    *,
    name: str,
    description: str,
    resource_filename: str,
) -> SkillMetadata:
    directory = _BUILTIN_SKILL_ROOT / name
    return SkillMetadata(
        name=name,
        description=description,
        content=_load_builtin_skill_content(resource_filename),
        directory=directory,
        entry_path=directory / resource_filename,
        origin="builtin",
    )


_BUILTIN_SKILLS: tuple[SkillMetadata, ...] = (
    _builtin_skill(
        name="git-master",
        description=_GIT_MASTER_DESCRIPTION,
        resource_filename="git-master.md",
    ),
    _builtin_skill(
        name="frontend-design",
        description=_FRONTEND_DESIGN_DESCRIPTION,
        resource_filename="frontend-design.md",
    ),
    _builtin_skill(
        name="playwright",
        description=_PLAYWRIGHT_DESCRIPTION,
        resource_filename="playwright.md",
    ),
    _builtin_skill(
        name="review-work",
        description=_REVIEW_WORK_DESCRIPTION,
        resource_filename="review-work.md",
    ),
    _builtin_skill(
        name="build-verification",
        description=_BUILD_VERIFICATION_DESCRIPTION,
        resource_filename="build-verification.md",
    ),
)


def list_builtin_skills() -> tuple[SkillMetadata, ...]:
    return _BUILTIN_SKILLS


def load_builtin_skill_registry() -> SkillRegistry:
    return SkillRegistry.from_skills(_BUILTIN_SKILLS)


def skill_registry_with_builtins(
    discovered_skills: Iterable[SkillMetadata],
) -> SkillRegistry:
    """Merge builtin skills with workspace-discovered skills.

    Workspace skills intentionally keep existing duplicate fail-closed semantics: a local skill with
    the same name as a builtin skill is rejected instead of silently overriding prompt guidance.
    """

    return SkillRegistry.from_skills((*_BUILTIN_SKILLS, *tuple(discovered_skills)))


__all__ = [
    "list_builtin_skills",
    "load_builtin_skill_registry",
    "skill_registry_with_builtins",
]
