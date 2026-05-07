from __future__ import annotations

import pytest

from tests.unit.conftest_cache_thresholds import (
    PROMPT_TOKEN_GROWTH_MAX_PCT,
    STABLE_PREFIX_MIN_RATIO,
)
from voidcode.agent.prompt_sections import dynamic_boundary_marker
from voidcode.agent.prompts import render_builtin_prompt_profile
from voidcode.runtime.context_window import count_text_tokens
from voidcode.runtime.prompt_assembly import PromptAssemblySection, build_prompt_assembly_plan

TOKENIZER_MODEL = "cl100k_base"
REPRESENTATIVE_SESSION_STATE = {
    "workspace_root": "/tmp/not-a-worktree",
    "model": "opencode/test-model",
}


def _prompt_tokens(value: str) -> int:
    return count_text_tokens(value, tokenizer_model=TOKENIZER_MODEL).tokens


def _rendered_prompt_parts(
    profile_name: str, *, prompt: str, tool_tail: str = ""
) -> tuple[int, int]:
    artifact_reference_sections = ()
    if tool_tail:
        artifact_reference_sections = (
            PromptAssemblySection(
                role="tool",
                content=tool_tail,
                source="tool_result",
                tier="recent",
            ),
        )

    plan = build_prompt_assembly_plan(
        prompt=prompt,
        runtime_instruction_precedence=(
            "Runtime instruction: satisfy the request, preserve user constraints, "
            "and verify the result."
        ),
        agent_prompt_context=render_builtin_prompt_profile(profile_name) or "",
        prompt_profile_name=profile_name,
        session_runtime_state=REPRESENTATIVE_SESSION_STATE,
        todo_prompt_context=(
            "Todo state: inspect the requested files, add the focused tests, "
            "run repeat verification."
        ),
        artifact_reference_sections=artifact_reference_sections,
    )
    rendered_messages = [section.content for section in plan.sections]
    boundary_marker = dynamic_boundary_marker()
    boundary_index = rendered_messages.index(boundary_marker)
    full_prompt = "\n\n".join(rendered_messages)
    stable_prefix = "\n\n".join(rendered_messages[: boundary_index + 1])
    return _prompt_tokens(full_prompt), _prompt_tokens(stable_prefix)


@pytest.mark.parametrize("profile_name", ("leader", "explore"))
def test_representative_prompt_growth_stays_within_budget(profile_name: str) -> None:
    baseline_tokens, _ = _rendered_prompt_parts(
        profile_name,
        prompt=f"{profile_name}: add focused prompt budget regression coverage.",
    )
    grown_tokens, _ = _rendered_prompt_parts(
        profile_name,
        prompt=f"{profile_name}: continue after inspecting the first pytest result.",
        tool_tail="pytest passed: tests/unit/runtime/test_prompt_token_budget.py -q",
    )

    growth_pct = ((grown_tokens - baseline_tokens) * 100) / baseline_tokens

    assert growth_pct <= PROMPT_TOKEN_GROWTH_MAX_PCT


@pytest.mark.parametrize("profile_name", ("leader", "explore"))
def test_stable_prefix_ratio_meets_representative_budget(profile_name: str) -> None:
    full_tokens, stable_prefix_tokens = _rendered_prompt_parts(
        profile_name,
        prompt=f"{profile_name}: add focused prompt budget regression coverage.",
        tool_tail="pytest passed: tests/unit/runtime/test_prompt_token_budget.py -q",
    )

    assert stable_prefix_tokens / full_tokens >= STABLE_PREFIX_MIN_RATIO
