from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.skills import SkillRuntimeContext, build_runtime_contexts
from voidcode.skills import (
    DEFAULT_SKILL_SEARCH_PATHS,
    LocalSkillMetadataLoader,
    SkillManifestFrontmatter,
    SkillManifestParseError,
    SkillMetadata,
    SkillRegistry,
    load_builtin_skill_registry,
    parse_skill_frontmatter,
    parse_skill_manifest,
    skill_registry_with_builtins,
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
    assert tuple(skill.origin for skill in skills) == ("workspace", "workspace")


def test_skill_metadata_rejects_invalid_origin(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    skill_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="origin must be one of: workspace, builtin"):
        _ = SkillMetadata(
            name="demo",
            description="Demo skill",
            content="# Demo",
            directory=skill_dir,
            entry_path=skill_dir / "SKILL.md",
            origin=cast(Any, "remote"),
        )


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

    with pytest.raises(ValueError, match=re.escape(str(entry_path.resolve()))):
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


def test_builtin_skill_registry_provides_workflow_skill_catalog() -> None:
    registry = load_builtin_skill_registry()

    assert set(registry.skills) == {
        "git-master",
        "frontend-design",
        "playwright",
        "review-work",
        "build-verification",
    }
    git_master_content = registry.resolve("git-master").content
    assert "name: git-master" in git_master_content
    assert "description: Help with git history, commit preparation" in git_master_content
    assert "Preserve hooks, approvals, and repository policy." in git_master_content
    assert "status, diff, log, show, blame, and bisect" in git_master_content
    assert "MUST USE for ANY git operations" not in git_master_content
    assert "name: frontend-design" in registry.resolve("frontend-design").content
    assert (
        "Guidance for distinctive, production-grade frontend UI/UX work"
        in registry.resolve("frontend-design").content
    )
    assert "when editing tools are available" in registry.resolve("frontend-design").content
    assert "# Playwright Browser Verification" in registry.resolve("playwright").content
    assert "descriptor/config-gated" in registry.resolve("playwright").content
    assert "Claude plugin" in registry.resolve("playwright").content
    assert (
        "# Review Work - VoidCode-Compatible Read-Only Review Guidance"
        in registry.resolve("review-work").content
    )
    assert "unsupported agents" in registry.resolve("review-work").content
    assert "# Build Verification" in registry.resolve("build-verification").content
    assert "CMake" in registry.resolve("build-verification").content
    assert "target_add_dependencies" in registry.resolve("build-verification").content
    unsupported_review_role = "or" + "acle"
    assert unsupported_review_role not in registry.resolve("review-work").content.lower()
    removed_placeholder = "Catalog-visible builtin skill metadata only"
    assert removed_placeholder not in registry.resolve("playwright").content
    removed_skill_names = {"coding" + "-guidance", "research" + "-guidance"}
    assert removed_skill_names.isdisjoint(registry.skills)


def test_builtin_skill_catalog_descriptions_stay_guidance_scoped() -> None:
    registry = load_builtin_skill_registry()

    descriptions = "\n".join(
        registry.resolve(skill_name).description for skill_name in registry.skills
    )

    assert "MUST USE" not in descriptions
    assert "all browser interactions" not in descriptions
    assert "Guidance for safe git workflows" in registry.resolve("git-master").description
    assert (
        "without assuming browser automation is always present"
        in registry.resolve("playwright").description
    )


def test_builtin_skill_registry_loads_content_from_local_markdown_resources() -> None:
    registry = load_builtin_skill_registry()

    for skill_name in registry.skills:
        skill = registry.resolve(skill_name)
        assert skill.entry_path == (
            Path("/builtin/voidcode/skills") / skill_name / f"{skill_name}.md"
        )
        assert skill.directory == Path("/builtin/voidcode/skills") / skill_name
        assert skill.origin == "builtin"
        assert skill.content.strip()
        assert "https://github.com" not in skill.entry_path.as_posix()

    assert len(registry.resolve("frontend-design").content.splitlines()) > 40
    assert len(registry.resolve("playwright").content.splitlines()) > 20
    assert len(registry.resolve("review-work").content.splitlines()) > 40


def test_builtin_skill_merge_rejects_workspace_duplicate() -> None:
    builtin = load_builtin_skill_registry().resolve("git-master")

    with pytest.raises(ValueError, match="duplicate skill name 'git-master' discovered"):
        _ = skill_registry_with_builtins((builtin,))


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
