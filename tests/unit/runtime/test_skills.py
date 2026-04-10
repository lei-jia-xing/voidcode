from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.skills import (
    DEFAULT_SKILL_SEARCH_PATHS,
    LocalSkillMetadataLoader,
    SkillRegistry,
    SkillRuntimeContext,
    parse_skill_frontmatter,
)


def test_parse_skill_frontmatter_returns_required_metadata() -> None:
    metadata = parse_skill_frontmatter(
        "---\nname: summarize\ndescription: Summarize selected files.\n---\n# Summarize\n"
    )

    assert metadata == {
        "name": "summarize",
        "description": "Summarize selected files.",
    }


def test_parse_skill_frontmatter_rejects_missing_required_fields() -> None:
    with pytest.raises(ValueError, match="missing required fields: description"):
        _ = parse_skill_frontmatter("---\nname: summarize\n---\n")


def test_skill_loader_discovers_local_skills_from_default_workspace_path(tmp_path: Path) -> None:
    skill_root = tmp_path / DEFAULT_SKILL_SEARCH_PATHS[0]
    summarize_dir = skill_root / "summarize"
    review_dir = skill_root / "review"
    summarize_dir.mkdir(parents=True)
    review_dir.mkdir(parents=True)
    (summarize_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize selected files.\n---\n# Summarize\n",
        encoding="utf-8",
    )
    (review_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review a code change.\n---\n# Review\n",
        encoding="utf-8",
    )

    skills = LocalSkillMetadataLoader().discover(workspace=tmp_path)

    assert tuple(skill.name for skill in skills) == ("review", "summarize")
    assert tuple(skill.description for skill in skills) == (
        "Review a code change.",
        "Summarize selected files.",
    )
    assert tuple(skill.directory for skill in skills) == (
        review_dir.resolve(),
        summarize_dir.resolve(),
    )
    assert tuple(skill.entry_path.name for skill in skills) == ("SKILL.md", "SKILL.md")


def test_skill_registry_discovers_and_resolves_skills(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize selected files.\n---\n# Summarize\n",
        encoding="utf-8",
    )

    registry = SkillRegistry.discover(workspace=tmp_path)

    assert tuple(registry.skills) == ("summarize",)
    assert registry.resolve("summarize").description == "Summarize selected files."


def test_skill_registry_builds_runtime_contexts_from_skill_bodies(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    skill_contents = (
        "---\n"
        "name: summarize\n"
        "description: Summarize selected files.\n"
        "---\n"
        "# Summarize\n"
        "Use concise bullet points.\n"
    )
    (skill_dir / "SKILL.md").write_text(
        skill_contents,
        encoding="utf-8",
    )

    registry = SkillRegistry.discover(workspace=tmp_path)

    assert registry.runtime_contexts() == (
        SkillRuntimeContext(
            name="summarize",
            description="Summarize selected files.",
            content="# Summarize\nUse concise bullet points.",
        ),
    )


def test_skill_loader_rejects_workspace_escape_search_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        _ = LocalSkillMetadataLoader().discover(workspace=tmp_path, search_paths=("../skills",))
