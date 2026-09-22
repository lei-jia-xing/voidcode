from __future__ import annotations

from voidcode.runtime.context.prompt_assembly import build_prompt_assembly_plan


def _contents(plan) -> list[str]:
    return [section.content for section in plan.sections]


def test_hook_guidance_appears_in_hook_layer() -> None:
    plan = build_prompt_assembly_plan(
        prompt="do work",
        runtime_instruction_precedence="runtime first",
        hook_guidance=["Use tabs for indentation."],
    )
    hook_sections = [s for s in plan.sections if s.metadata.get("layer") == "hook_injected_context"]
    assert any("Use tabs for indentation." in s.content for s in hook_sections)


def test_hook_guidance_oversize_truncated_with_marker() -> None:
    long_item = "x" * 5000
    plan = build_prompt_assembly_plan(
        prompt="do work",
        runtime_instruction_precedence="runtime first",
        hook_guidance=[long_item],
    )
    hook_sections = [s for s in plan.sections if s.metadata.get("layer") == "hook_injected_context"]
    assert hook_sections
    assert any("…[truncated]" in s.content for s in hook_sections)
    assert all(len(s.content) < len(long_item) for s in hook_sections)


def test_hook_guidance_empty_leaves_prompt_unchanged() -> None:
    baseline = build_prompt_assembly_plan(
        prompt="do work",
        runtime_instruction_precedence="runtime first",
    )
    with_empty = build_prompt_assembly_plan(
        prompt="do work",
        runtime_instruction_precedence="runtime first",
        hook_guidance=[],
    )
    assert _contents(with_empty) == _contents(baseline)
