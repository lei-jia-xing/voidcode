from __future__ import annotations

import pytest

from voidcode.agent import AgentPromptMaterialization, get_builtin_agent_manifest, render_agent_prompt


def test_typed_package_body_precedes_append_and_does_not_select_builtin_profile() -> None:
    materialization = AgentPromptMaterialization(
        profile="leader",
        version=1,
        source="custom_markdown",
        format="markdown",
        body="  package-owned persona  ",
        prompt_append="  user-authored constraint  ",
    )
    rendered = render_agent_prompt(materialization)
    assert rendered is not None
    assert rendered.startswith("package-owned persona")
    assert rendered.endswith("user-authored constraint")
    assert rendered.index("package-owned persona") < rendered.index("user-authored constraint")


def test_typed_custom_body_without_append_is_not_replaced_by_profile() -> None:
    materialization = AgentPromptMaterialization(
        profile="worker",
        version=1,
        source="custom_markdown",
        format="markdown",
        body="  actual custom persona  ",
    )
    assert render_agent_prompt(materialization) == "actual custom persona"


def test_selecting_genuine_builtin_materializations_changes_active_persona() -> None:
    leader = get_builtin_agent_manifest("leader")
    worker = get_builtin_agent_manifest("worker")
    assert leader is not None and worker is not None
    assert leader.prompt_materialization is not None and worker.prompt_materialization is not None
    assert render_agent_prompt(leader.prompt_materialization) != render_agent_prompt(worker.prompt_materialization)


def test_unknown_builtin_materialization_refuses_instead_of_generating_persona() -> None:
    with pytest.raises(ValueError):
        render_agent_prompt(AgentPromptMaterialization(profile="not-a-builtin-profile", version=1))
