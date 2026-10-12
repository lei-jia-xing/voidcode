from __future__ import annotations

from voidcode.hook.presets import resolve_hook_preset_refs
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


def test_agent_prompt_uses_one_instruction_layer() -> None:
    plan = build_prompt_assembly_plan(
        prompt="request",
        runtime_instruction_precedence="runtime policy is authoritative",
        agent_prompt_context="Use tools when useful.",
    )
    agent_sections = [section for section in plan.sections if section.source.startswith("agent_")]
    assert [(section.source, section.metadata["layer"]) for section in agent_sections] == [("agent_prompt", "agent_instructions")]


def test_hook_guidance_is_plaintext_while_snapshot_metadata_remains_structured() -> None:
    snapshot = resolve_hook_preset_refs(("delegation_guard",))
    context = snapshot.guidance_context()
    assert context
    assert "<hook_presets>" not in context
    assert "<event_scopes>" not in context
    assert "<allowed_actions>" not in context
    payload = snapshot.to_payload()
    assert payload["refs"] == ["delegation_guard"]
    assert payload["presets"][0]["event_scopes"]
    assert payload["presets"][0]["allowed_actions"]


def test_explicit_hook_refs_remain_resolvable() -> None:
    snapshot = resolve_hook_preset_refs(("role_reminder",))
    assert snapshot.refs == ("role_reminder",)
    assert snapshot.guidance_context()
