from __future__ import annotations

from voidcode.agent.prompt_sections import user_append_heading_block
from voidcode.agent.prompts import compose_prompt_with_user_append, render_agent_prompt, render_builtin_prompt_profile
from voidcode.runtime.context.prompt_assembly import (
    _BASE_SAFETY_GUIDANCE,
    build_prompt_assembly_plan,
)


def test_compose_both_present_includes_heading_in_order() -> None:
    composed = compose_prompt_with_user_append("generated body", "user text")

    heading = user_append_heading_block()
    assert composed.index("generated body") < composed.index(heading) < composed.index("user text")


def test_compose_either_empty_has_no_heading() -> None:
    heading = user_append_heading_block()

    assert compose_prompt_with_user_append("generated body", "") == "generated body"
    assert compose_prompt_with_user_append("", "user text") == "user text"
    assert compose_prompt_with_user_append("  ", "  ") == ""
    assert heading not in compose_prompt_with_user_append("generated body", "")
    assert heading not in compose_prompt_with_user_append("", "user text")


def test_render_agent_prompt_custom_path_uses_heading() -> None:
    heading = user_append_heading_block()
    prompt = render_agent_prompt(
        {
            "prompt_materialization": {
                "source": "custom_markdown",
                "body": "generated body",
                "prompt_append": "user text",
            }
        }
    )

    assert prompt is not None
    assert prompt.index("generated body") < prompt.index(heading) < prompt.index("user text")


def test_safety_guidance_stays_first_with_profile_overlay() -> None:
    plan = build_prompt_assembly_plan(
        prompt="do work",
        runtime_instruction_precedence="runtime first",
        agent_prompt_context="agent ctx",
        prompt_profile_name="worker",
    )

    assert plan.sections
    assert plan.sections[0].content == _BASE_SAFETY_GUIDANCE
    assert plan.sections[0].source == "runtime_base_safety"


def test_builtin_tails_reference_profile_contracts() -> None:
    leader = render_builtin_prompt_profile("leader")
    explore = render_builtin_prompt_profile("explore")
    researcher = render_builtin_prompt_profile("researcher")
    worker = render_builtin_prompt_profile("worker")
    advisor = render_builtin_prompt_profile("advisor")
    product = render_builtin_prompt_profile("product")

    assert leader is not None and "delegation envelope" in leader.lower()
    assert explore is not None and "search contract" in explore.lower()
    assert researcher is not None and "search contract" in researcher.lower()
    assert worker is not None and "stay in scope and yield" in worker.lower()
    assert advisor is not None and "stay in scope and yield" in advisor.lower()
    assert product is not None and "stay in scope and yield" in product.lower()
