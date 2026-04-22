from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.skills import (
    SkillRuntimeContext,
    build_runtime_contexts,
    build_skill_execution_snapshot,
    build_skill_prompt_context,
    snapshot_from_payload,
    snapshot_payload,
)
from voidcode.skills import (
    DEFAULT_SKILL_SEARCH_PATHS,
    LocalSkillMetadataLoader,
    SkillRegistry,
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


def test_skill_runtime_context_builds_execution_prompt_context(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: summarize\n"
        "description: Summarize selected files.\n"
        "---\n"
        "# Summarize\n"
        "Use concise bullet points.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry.discover(workspace=tmp_path)

    contexts = build_runtime_contexts(registry, skill_names=("summarize",))

    assert contexts[0].prompt_context == (
        "Skill: summarize\n"
        "Description: Summarize selected files.\n"
        "Instructions:\n# Summarize\nUse concise bullet points."
    )
    assert build_skill_prompt_context(contexts) == (
        "Runtime-managed skills are active for this turn. "
        "Apply these instructions in addition to the user's request.\n\n"
        "Skill: summarize\n"
        "Description: Summarize selected files.\n"
        "Instructions:\n# Summarize\nUse concise bullet points."
    )


def test_skill_loader_rejects_workspace_escape_search_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes workspace"):
        _ = LocalSkillMetadataLoader().discover(workspace=tmp_path, search_paths=("../skills",))


def test_build_skill_execution_snapshot_stable_payload() -> None:
    contexts = (
        SkillRuntimeContext(
            name="demo",
            description="Demo",
            content="# Demo\nUse it.",
            prompt_context="Skill: demo\nDescription: Demo\nInstructions:\n# Demo\nUse it.",
            execution_notes="# Demo\nUse it.",
            source_path="C:/demo/SKILL.md",
        ),
    )

    first = build_skill_execution_snapshot(contexts, source="run")
    second = build_skill_execution_snapshot(contexts, source="run")

    assert first.snapshot_hash == second.snapshot_hash
    assert snapshot_payload(first) == snapshot_payload(second)


def test_snapshot_roundtrip_from_payload() -> None:
    contexts = (
        SkillRuntimeContext(
            name="demo",
            description="Demo",
            content="# Demo\nUse it.",
            prompt_context="Skill: demo\nDescription: Demo\nInstructions:\n# Demo\nUse it.",
            execution_notes="# Demo\nUse it.",
            source_path="C:/demo/SKILL.md",
        ),
    )
    snapshot = build_skill_execution_snapshot(contexts, source="run")

    restored = snapshot_from_payload(snapshot_payload(snapshot))

    assert restored.selected_skill_names == snapshot.selected_skill_names
    assert restored.applied_skill_payloads == snapshot.applied_skill_payloads
    assert restored.skill_prompt_context == snapshot.skill_prompt_context
    assert restored.snapshot_hash == snapshot.snapshot_hash


def test_snapshot_from_payload_recomputes_stale_hash() -> None:
    contexts = (
        SkillRuntimeContext(
            name="demo",
            description="Demo",
            content="# Demo\nUse it.",
            prompt_context="Skill: demo\nDescription: Demo\nInstructions:\n# Demo\nUse it.",
            execution_notes="# Demo\nUse it.",
            source_path="C:/demo/SKILL.md",
        ),
    )
    snapshot = build_skill_execution_snapshot(contexts, source="run")
    payload = snapshot_payload(snapshot)
    payload["snapshot_hash"] = "stale-hash"

    restored = snapshot_from_payload(payload)

    assert restored.snapshot_hash == snapshot.snapshot_hash
