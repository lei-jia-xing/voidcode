from __future__ import annotations

from pathlib import Path

from voidcode.runtime.skill_metadata import (
    available_runtime_contexts,
    catalog_skill_context,
)
from voidcode.skills import (
    DEFAULT_SKILL_SEARCH_PATHS,
    LocalSkillMetadataLoader,
    SkillRegistry,
)

_SKILL_BODY_SECRET = "INSTRUCTIONS BODY THAT MUST NOT LEAK INTO CATALOG"


def _make_workspace_with_skills(tmp_path: Path) -> SkillRegistry:
    skill_root = tmp_path / DEFAULT_SKILL_SEARCH_PATHS[0]
    summarize_dir = skill_root / "summarize"
    review_dir = skill_root / "review"
    summarize_dir.mkdir(parents=True)
    review_dir.mkdir(parents=True)
    (summarize_dir / "SKILL.md").write_text(
        "---\n"
        "name: summarize\n"
        "description: Summarize selected files cheaply.\n"
        "---\n"
        f"# Summarize body\n{_SKILL_BODY_SECRET}\nstep one of summarize.\n",
        encoding="utf-8",
    )
    (review_dir / "SKILL.md").write_text(
        "---\n"
        "name: review\n"
        "description: Review changes against architecture rules.\n"
        "---\n"
        f"# Review body\n{_SKILL_BODY_SECRET}\nstep one of review.\n",
        encoding="utf-8",
    )
    discovered = LocalSkillMetadataLoader().discover(workspace=tmp_path)
    return SkillRegistry(skills={skill.name: skill for skill in discovered})


def test_catalog_skill_context_omits_full_skill_body(tmp_path: Path) -> None:
    """Lazy harness contract: the catalog must never embed the SKILL.md body.

    If this test starts failing, the lazy-loading contract has been broken and
    every active session is now paying the full body cost upfront. That is the
    opposite of the OhMyOpenCode/OpenCode lazy-skill design.
    """
    registry = _make_workspace_with_skills(tmp_path)

    catalog = catalog_skill_context(
        registry,
        available_skill_names=("summarize", "review"),
        selected_skill_names=(),
    )

    assert _SKILL_BODY_SECRET not in catalog
    assert "step one of summarize" not in catalog
    assert "step one of review" not in catalog


def test_catalog_skill_context_includes_name_and_description(tmp_path: Path) -> None:
    registry = _make_workspace_with_skills(tmp_path)

    catalog = catalog_skill_context(
        registry,
        available_skill_names=("summarize", "review"),
        selected_skill_names=(),
    )

    assert "<name>summarize</name>" in catalog
    assert "Summarize selected files cheaply." in catalog
    assert "<name>review</name>" in catalog
    assert "Review changes against architecture rules." in catalog


def test_catalog_skill_context_directs_model_to_lazy_load_via_tool(
    tmp_path: Path,
) -> None:
    registry = _make_workspace_with_skills(tmp_path)

    catalog = catalog_skill_context(
        registry,
        available_skill_names=("summarize",),
        selected_skill_names=(),
    )

    # The catalog must explicitly tell the model how to fetch a skill body
    # rather than letting the model invent its own loading semantics.
    assert "skill(name=" in catalog
    # The catalog must discourage speculative bulk loading; this is the
    # behavioral knob that keeps the harness lightweight.
    assert "speculatively" in catalog.lower() or "lazy" in catalog.lower()


def test_catalog_skill_context_returns_empty_when_no_skills() -> None:
    empty_registry = SkillRegistry(skills={})

    catalog = catalog_skill_context(
        empty_registry,
        available_skill_names=(),
        selected_skill_names=(),
    )

    assert catalog == ""


def test_available_runtime_contexts_does_load_full_body_when_requested(
    tmp_path: Path,
) -> None:
    """When the runtime explicitly opts into eager loading (e.g., force_load_skills
    or skill tool invocation), the body MUST become available. This protects the
    other half of the contract: lazy by default, eager on request.
    """
    registry = _make_workspace_with_skills(tmp_path)

    contexts = available_runtime_contexts(registry, ("summarize",))

    assert len(contexts) == 1
    only = contexts[0]
    assert only.name == "summarize"
    assert _SKILL_BODY_SECRET in only.content
    assert "step one of summarize" in only.content
