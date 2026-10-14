from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from voidcode.agent import AgentPromptMaterialization, get_builtin_agent_manifest, render_agent_prompt
from voidcode.agent import prompts as agent_prompts


def test_typed_package_body_precedes_append_without_builtin_prompt_selection() -> None:
    materialization = AgentPromptMaterialization(
        profile="leader",
        version=1,
        source="custom_markdown",
        format="markdown",
        body="  package-owned prompt  ",
        prompt_append="  user-authored constraint  ",
    )
    rendered = render_agent_prompt(materialization)
    assert rendered is not None
    assert rendered.startswith("package-owned prompt")
    assert rendered.endswith("user-authored constraint")
    assert rendered.index("package-owned prompt") < rendered.index("user-authored constraint")


def test_typed_custom_prompt_without_append_is_not_replaced_by_profile() -> None:
    materialization = AgentPromptMaterialization(
        profile="worker",
        version=1,
        source="custom_markdown",
        format="markdown",
        body="  actual custom prompt  ",
    )
    assert render_agent_prompt(materialization) == "actual custom prompt"


def test_selecting_genuine_builtin_materializations_changes_active_prompts() -> None:
    default = get_builtin_agent_manifest("default")
    execute = get_builtin_agent_manifest("execute")
    assert default is not None and execute is not None
    assert default.prompt_materialization is not None and execute.prompt_materialization is not None
    assert render_agent_prompt(default.prompt_materialization) != render_agent_prompt(execute.prompt_materialization)


def test_unknown_builtin_materialization_refuses_instead_of_generating_prompt() -> None:
    with pytest.raises(ValueError):
        render_agent_prompt(AgentPromptMaterialization(profile="not-a-builtin-profile", version=1))


def test_builtin_prompt_profiles_load_from_zip_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path = tmp_path / "agent-prompts.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("default/base.txt", " zip-backed default prompt ")

    monkeypatch.setattr(agent_prompts, "files", lambda _package: zipfile.Path(archive_path))
    agent_prompts._render_known_builtin_prompt_profile.cache_clear()
    try:
        assert agent_prompts.has_builtin_prompt_profile("default")
        assert agent_prompts.render_builtin_prompt_profile("default") == "zip-backed default prompt"
    finally:
        agent_prompts._render_known_builtin_prompt_profile.cache_clear()
