from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.skills import SkillRuntimeContext, build_runtime_contexts
from voidcode.skills import (
    DEFAULT_SKILL_SEARCH_PATHS,
    LocalSkillMetadataLoader,
    SkillManifestFrontmatter,
    SkillManifestParseError,
    SkillRegistry,
    parse_skill_frontmatter,
    parse_skill_manifest,
)


def test_parse_skill_frontmatter_returns_required_metadata() -> None:
    metadata = parse_skill_frontmatter(
        "---\nname: summarize\ndescription: Summarize selected files.\n---\n# Summarize\n"
    )

    assert metadata == SkillManifestFrontmatter(
        name="summarize",
        description="Summarize selected files.",
    )


def test_parse_skill_frontmatter_rejects_missing_required_fields() -> None:
    with pytest.raises(SkillManifestParseError, match="missing required fields: description"):
        _ = parse_skill_frontmatter("---\nname: summarize\n---\n")


def test_parse_skill_manifest_rejects_empty_body() -> None:
    with pytest.raises(SkillManifestParseError, match="content must be a non-empty string"):
        _ = parse_skill_manifest("---\nname: summarize\ndescription: Demo\n---\n")


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


def test_skill_loader_discovers_nested_skill_directories(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "python" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize selected files.\n---\n# Summarize\n",
        encoding="utf-8",
    )

    skills = LocalSkillMetadataLoader().discover(workspace=tmp_path)

    assert [skill.name for skill in skills] == ["summarize"]
    assert skills[0].directory == skill_dir.resolve()


def test_skill_loader_reports_entry_path_in_parse_errors(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    entry_path = skill_dir / "SKILL.md"
    entry_path.write_text("---\nname: broken\n---\n# Missing description\n", encoding="utf-8")

    with pytest.raises(ValueError, match=str(entry_path.resolve()).replace(".", r"\.")):
        _ = LocalSkillMetadataLoader().load(entry_path)


def test_skill_registry_rejects_duplicate_skill_names(tmp_path: Path) -> None:
    alpha = tmp_path / ".voidcode" / "skills" / "alpha"
    beta = tmp_path / ".voidcode" / "skills" / "nested" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    (alpha / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Alpha demo.\n---\n# Alpha\n",
        encoding="utf-8",
    )
    (beta / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Beta demo.\n---\n# Beta\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate skill name 'demo' discovered"):
        _ = SkillRegistry.discover(workspace=tmp_path)


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


def test_runtime_build_runtime_contexts_from_skill_bodies(tmp_path: Path) -> None:
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

    assert build_runtime_contexts(registry) == (
        SkillRuntimeContext(
            name="summarize",
            description="Summarize selected files.",
            content="# Summarize\nUse concise bullet points.",
            prompt_context=(
                "Skill: summarize\n"
                "Description: Summarize selected files.\n"
                "Instructions:\n# Summarize\nUse concise bullet points."
            ),
            execution_notes="# Summarize\nUse concise bullet points.",
            source_path=str((skill_dir / "SKILL.md").resolve()),
        ),
    )


def test_skill_loader_rejects_workspace_escape_search_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        _ = LocalSkillMetadataLoader().discover(workspace=tmp_path, search_paths=("../skills",))
