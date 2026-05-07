from __future__ import annotations

from hashlib import sha256

from voidcode.agent.prompt_sections import dynamic_boundary_marker
from voidcode.runtime.prompt_assembly import PromptAssemblySection, build_prompt_assembly_plan


def compute_stable_prefix_hash(rendered_messages: list[str], boundary_marker: str) -> str:
    """Hash the stable prefix of a rendered prompt transcript.

    Assumptions:
    - `rendered_messages` is already ordered exactly as it will be rendered.
    - `boundary_marker` appears once and marks the last stable element.
    - The hash covers the bytes from the first message through the boundary marker,
      joined with newline separators, and excludes any later dynamic messages.
    """

    assert rendered_messages.count(boundary_marker) == 1
    boundary_index = rendered_messages.index(boundary_marker)
    stable_prefix = "\n".join(rendered_messages[: boundary_index + 1]).encode("utf-8")
    return sha256(stable_prefix).hexdigest()


def test_compute_stable_prefix_hash_uses_prefix_through_boundary_marker() -> None:
    boundary_marker = dynamic_boundary_marker()
    rendered_messages = ["stable-a", "stable-b", boundary_marker, "dynamic-a"]

    assert (
        compute_stable_prefix_hash(rendered_messages, boundary_marker)
        == sha256("\n".join(["stable-a", "stable-b", boundary_marker]).encode("utf-8")).hexdigest()
    )


def test_same_session_prompts_with_different_user_and_tool_tail_share_prefix_hash() -> None:
    boundary_marker = dynamic_boundary_marker()
    same_session_state = {
        "workspace_root": "/tmp/not-a-worktree",
        "model": "opencode/test-model",
    }
    first_plan = build_prompt_assembly_plan(
        prompt="summarize the first result",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="leader base body",
        prompt_profile_name="leader",
        session_runtime_state=same_session_state,
        artifact_reference_sections=(
            PromptAssemblySection(
                role="tool",
                content="tool: first result",
                source="tool_result",
                tier="recent",
            ),
        ),
    )
    second_plan = build_prompt_assembly_plan(
        prompt="summarize the second result",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="leader base body",
        prompt_profile_name="leader",
        session_runtime_state=same_session_state,
        artifact_reference_sections=(
            PromptAssemblySection(
                role="tool",
                content="tool: second result",
                source="tool_result",
                tier="recent",
            ),
        ),
    )
    first_prompt = [section.content for section in first_plan.sections]
    second_prompt = [section.content for section in second_plan.sections]
    first_boundary_index = first_prompt.index(boundary_marker)
    second_boundary_index = second_prompt.index(boundary_marker)

    assert first_prompt.count(boundary_marker) == 1
    assert second_prompt.count(boundary_marker) == 1
    assert compute_stable_prefix_hash(first_prompt, boundary_marker) == compute_stable_prefix_hash(
        second_prompt,
        boundary_marker,
    )
    assert first_prompt[first_boundary_index + 1 :] != second_prompt[second_boundary_index + 1 :]
