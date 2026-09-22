from __future__ import annotations

from voidcode.hook.executor import HookExecutionEvent, HookExecutionOutcome
from voidcode.runtime.context.prompt_assembly import build_prompt_assembly_plan
from voidcode.runtime.hook_runtime import hook_guidance_from_outcome


def _outcome(*payloads: dict[str, object], failed_error: str | None = None) -> HookExecutionOutcome:
    events = tuple(
        HookExecutionEvent(sequence=index + 1, event_type="runtime.tool_hook.pre", payload=dict(payload)) for index, payload in enumerate(payloads)
    )
    return HookExecutionOutcome(events=events, last_sequence=len(events), failed_error=failed_error)


def _hook_layer_contents(plan) -> list[str]:
    return [section.content for section in plan.sections if section.metadata.get("layer") == "hook_injected_context"]


def test_producer_feeds_stubbed_guidance_into_prompt() -> None:
    outcome = _outcome({"phase": "pre", "tool_name": "read", "guidance": "Prefer tabs over spaces."})
    guidance = hook_guidance_from_outcome(outcome)
    plan = build_prompt_assembly_plan(
        prompt="do work",
        runtime_instruction_precedence="runtime first",
        hook_guidance=guidance or None,
    )
    assert any("Prefer tabs over spaces." in content for content in _hook_layer_contents(plan))


def test_hook_error_outcome_leaves_prompt_unchanged() -> None:
    baseline = build_prompt_assembly_plan(prompt="do work", runtime_instruction_precedence="runtime first")
    outcome = _outcome(failed_error="tool pre-hook failed for read: boom")
    guidance = hook_guidance_from_outcome(outcome)
    assert guidance == ()
    plan = build_prompt_assembly_plan(
        prompt="do work",
        runtime_instruction_precedence="runtime first",
        hook_guidance=guidance or None,
    )
    assert [section.content for section in plan.sections] == [section.content for section in baseline.sections]


def test_diagnostics_are_not_guidance() -> None:
    outcome = HookExecutionOutcome(
        events=(),
        last_sequence=0,
        diagnostics=("hook saw the read call",),
    )
    assert hook_guidance_from_outcome(outcome) == ()


def test_non_string_guidance_ignored_fail_open() -> None:
    outcome = _outcome({"phase": "pre", "guidance": {"nested": "object"}}, {"phase": "post", "guidance": ""})
    assert hook_guidance_from_outcome(outcome) == ()
