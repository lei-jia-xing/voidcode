from __future__ import annotations

from voidcode.runtime.context_transforms import (
    RuntimeContextTransformInjection,
    RuntimeContextTransformResult,
)
from voidcode.runtime.prompt_assembly import (
    PromptAssemblySection,
    build_prompt_assembly_plan,
)


def test_build_prompt_assembly_plan_orders_core_sections() -> None:
    plan = build_prompt_assembly_plan(
        prompt="fix the failing test",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="agent prompt",
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
