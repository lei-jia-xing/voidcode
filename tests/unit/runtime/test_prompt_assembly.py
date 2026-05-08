from __future__ import annotations

from voidcode.agent.prompt_sections import (
    assemble_sections,
    capability_block,
    delegation_envelope_block,
    dynamic_boundary_marker,
    env_card_dynamic,
    env_card_stable,
    identity_header,
    search_agent_contract_block,
)
from voidcode.agent.prompts import render_builtin_prompt_profile
from voidcode.runtime.context_transforms import (
    RuntimeContextTransformInjection,
    RuntimeContextTransformResult,
)
from voidcode.runtime.prompt_assembly import (
    PromptAssemblySection,
    build_prompt_assembly_plan,
)

BUILTIN_PROMPT_PROFILES = (
    "leader",
    "product",
    "worker",
    "advisor",
    "explore",
    "researcher",
)

DELEGATION_PROFILES = {"leader", "product"}
SEARCH_PROFILES = {"explore", "researcher"}
PROVIDER_SPECIFIC_PROMPT_TERMS = (
    "Anthropic",
    "Claude",
    "DeepSeek",
    "LiteLLM",
    "cache_control",
)


def test_build_prompt_assembly_plan_orders_core_sections() -> None:
    plan = build_prompt_assembly_plan(
        prompt="fix the failing test",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="agent prompt",
        workflow_mode_prompt_context="workflow mode prompt",
        preserved_system_segments=("preserved-a",),
        skill_prompt_context="skill context",
        pending_state_section=PromptAssemblySection(
            role="system",
            content="waiting approval",
            source="runtime_pending_state",
            tier="task",
            metadata={"status": "waiting_approval"},
        ),
        todo_prompt_context="active todo",
        continuity_summary="continuity summary",
        artifact_reference_sections=(
            PromptAssemblySection(
                role="system",
                content="artifact ref",
                source="runtime_context_artifact_reference",
                tier="recent",
            ),
        ),
    )

    assert [section.source for section in plan.sections] == [
        "runtime_instruction_precedence",
        "agent_prompt",
        "workflow_mode_prompt",
        "preserved_system_segment",
        "skill_prompt",
        "runtime_pending_state",
        "runtime_todo_state",
        "continuity_summary",
        "runtime_context_artifact_reference",
        "current_user_prompt",
    ]
    assert plan.sections[-1].role == "user"
    assert plan.sections[-1].content == "fix the failing test"
    assert [section.tier for section in plan.sections] == [
        "instruction",
        "instruction",
        "instruction",
        "instruction",
        "instruction",
        "task",
        "task",
        "recent",
        "recent",
        "task",
    ]


def test_build_prompt_assembly_plan_deduplicates_system_text() -> None:
    plan = build_prompt_assembly_plan(
        prompt="continue",
        runtime_instruction_precedence="same text",
        agent_prompt_context="same text",
        workflow_mode_prompt_context="same text",
        preserved_system_segments=("same text",),
        skill_prompt_context="same text",
    )

    assert [section.source for section in plan.sections] == [
        "runtime_instruction_precedence",
        "current_user_prompt",
    ]


def test_build_prompt_assembly_plan_keeps_non_system_transform_roles() -> None:
    plan = build_prompt_assembly_plan(
        prompt="continue",
        runtime_instruction_precedence="runtime first",
        context_transform_result=RuntimeContextTransformResult(
            injections=(
                RuntimeContextTransformInjection(
                    role="assistant",
                    content="assistant injected note",
                    metadata={"source": "transform_assistant", "tier": "workspace"},
                ),
                RuntimeContextTransformInjection(
                    role="system",
                    content="system injected note",
                    metadata={"source": "transform_system", "tier": "workspace"},
                ),
            )
        ),
    )

    assert [section.source for section in plan.sections] == [
        "runtime_instruction_precedence",
        "transform_assistant",
        "transform_system",
        "current_user_prompt",
    ]
    assistant_section = plan.sections[1]
    assert assistant_section.role == "assistant"
    assert assistant_section.content == "assistant injected note"
    assert assistant_section.tier == "workspace"


def test_build_prompt_assembly_plan_preserves_pending_state_metadata() -> None:
    pending = PromptAssemblySection(
        role="system",
        content="Runtime pending state: waiting_question.",
        source="runtime_pending_state",
        tier="task",
        metadata={"status": "waiting_question", "blocked_tool": "question"},
    )
    plan = build_prompt_assembly_plan(
        prompt="continue",
        runtime_instruction_precedence="runtime first",
        pending_state_section=pending,
    )

    pending_section = plan.sections[1]
    assert pending_section.source == "runtime_pending_state"
    assert pending_section.metadata == {
        "status": "waiting_question",
        "blocked_tool": "question",
    }
    assert pending_section.tier == "task"


def test_build_prompt_assembly_plan_composes_stable_prefix_before_dynamic_suffix() -> None:
    plan = build_prompt_assembly_plan(
        prompt="fix the failing test",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="leader base body",
        workflow_mode_prompt_context="workflow mode prompt",
        prompt_profile_name="leader",
        session_runtime_state={
            "workspace_root": "/tmp/not-a-worktree",
            "model": "opencode/test-model",
        },
        todo_prompt_context="active todo",
    )

    sources = [section.source for section in plan.sections]
    boundary_index = sources.index("runtime_dynamic_boundary")

    assert sources[:boundary_index] == [
        "agent_identity_header",
        "agent_capability_block",
        "agent_prompt",
        "agent_profile_overlay",
        "runtime_environment_stable",
    ]
    assert sources[boundary_index + 1 :] == [
        "runtime_environment_dynamic",
        "runtime_instruction_precedence",
        "workflow_mode_prompt",
        "runtime_todo_state",
        "current_user_prompt",
    ]
    assert plan.sections[boundary_index].content == dynamic_boundary_marker()
    assert sum(section.content == dynamic_boundary_marker() for section in plan.sections) == 1
    assert sum(section.content == "leader base body" for section in plan.sections) == 1
    assert all("Git status:" not in section.content for section in plan.sections[:boundary_index])
    assert "Git status:" not in plan.sections[boundary_index].content


def test_build_prompt_assembly_plan_adds_search_contract_only_for_search_profiles() -> None:
    explore_plan = build_prompt_assembly_plan(
        prompt="map the repo",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="explore base body",
        workflow_mode_prompt_context="workflow mode prompt",
        prompt_profile_name="explore",
        session_runtime_state={"model": "opencode/test-model"},
    )
    worker_plan = build_prompt_assembly_plan(
        prompt="make the edit",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="worker base body",
        workflow_mode_prompt_context="workflow mode prompt",
        prompt_profile_name="worker",
        session_runtime_state={"model": "opencode/test-model"},
    )

    assert any(
        section.source == "agent_profile_overlay" and "<search_agent_contract>" in section.content
        for section in explore_plan.sections
    )
    assert not any("<search_agent_contract>" in section.content for section in worker_plan.sections)
    assert not any("<delegation_envelope>" in section.content for section in worker_plan.sections)


def test_builtin_prompt_profiles_render_with_expected_overlay_boundaries() -> None:
    for profile in BUILTIN_PROMPT_PROFILES:
        base_prompt = render_builtin_prompt_profile(profile)
        assert base_prompt is not None

        plan = build_prompt_assembly_plan(
            prompt=f"exercise {profile}",
            runtime_instruction_precedence="runtime first",
            agent_prompt_context=base_prompt,
            prompt_profile_name=profile,
            session_runtime_state={"model": "opencode/test-model"},
        )
        rendered_system_text = "\n\n".join(
            section.content for section in plan.sections if section.role == "system"
        )

        assert rendered_system_text.count(base_prompt) == 1
        assert ("<delegation_envelope>" in rendered_system_text) is (profile in DELEGATION_PROFILES)
        assert ("<search_agent_contract>" in rendered_system_text) is (profile in SEARCH_PROFILES)


def test_general_prompt_sections_are_provider_neutral() -> None:
    general_sections = (
        identity_header("worker", "Runtime role summary."),
        capability_block(["Inspect context.", "Verify results."]),
        env_card_stable("Linux", "neutral-model"),
        env_card_dynamic("/workspace", "2026-05-08", "clean"),
        delegation_envelope_block(),
        search_agent_contract_block(),
        dynamic_boundary_marker(),
        assemble_sections(["stable section"], ["dynamic section"], dynamic_boundary_marker()),
    )

    rendered_general_sections = "\n\n".join(general_sections)
    for provider_term in PROVIDER_SPECIFIC_PROMPT_TERMS:
        assert provider_term not in rendered_general_sections
