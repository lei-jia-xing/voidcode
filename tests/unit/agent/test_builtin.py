from __future__ import annotations

import pytest

from voidcode.agent import (
    get_builtin_agent_manifest,
    list_builtin_agent_manifests,
    render_builtin_prompt_profile,
)
from voidcode.agent.builtin import validate_builtin_agent_manifests
from voidcode.agent.models import AgentManifest, AgentPromptMaterialization

_READ_ONLY_AGENT_PRESETS = ("advisor", "explore", "researcher", "product")
_DELEGATED_ONLY_AGENT_PRESETS = ("worker", "advisor", "explore", "researcher", "product")
_CALLABLE_CHILD_AGENT_PRESETS = _DELEGATED_ONLY_AGENT_PRESETS
_MUTATING_TOOL_PATTERNS = frozenset(
    {
        "write",
        "edit",
        "multi_edit",
        "apply_patch",
        "shell_exec",
        "ast_grep_replace",
        "task",
    }
)


def test_builtin_agent_manifests_have_materialized_prompt_profiles_and_execution_engines() -> None:
    manifests = list_builtin_agent_manifests()

    assert manifests
    for manifest in manifests:
        assert manifest.prompt_profile is not None
        assert manifest.prompt_materialization is not None
        assert manifest.prompt_materialization.profile == manifest.prompt_profile
        assert manifest.prompt_materialization.source == "builtin"
        assert manifest.prompt_materialization.format == "text"
        assert manifest.prompt_materialization.version >= 1
        assert manifest.execution_engine == "provider"
        prompt = render_builtin_prompt_profile(manifest.prompt_profile)
        assert prompt is not None
        assert prompt


def test_builtin_agent_manifests_declare_top_level_selectability() -> None:
    manifests = list_builtin_agent_manifests()

    assert [manifest.id for manifest in manifests if manifest.top_level_selectable] == [
        "leader",
    ]


def test_builtin_delegated_only_agent_manifests_are_not_top_level_selectable() -> None:
    for preset in _DELEGATED_ONLY_AGENT_PRESETS:
        manifest = get_builtin_agent_manifest(preset)

        assert manifest is not None
        assert manifest.mode == "subagent"
        assert manifest.top_level_selectable is False


def test_builtin_subagent_tool_allowlists_enforce_role_boundaries() -> None:
    write_tools = {"write", "edit", "multi_edit", "apply_patch"}

    for preset in ("advisor", "explore"):
        manifest = get_builtin_agent_manifest(preset)
        assert manifest is not None
        assert write_tools.isdisjoint(manifest.tool_allowlist)
        assert "task" not in manifest.tool_allowlist
        assert "background_task" not in manifest.tool_allowlist
        assert "question" not in manifest.tool_allowlist

    worker = get_builtin_agent_manifest("worker")
    assert worker is not None
    assert write_tools.issubset(worker.tool_allowlist)
    assert "task" not in worker.tool_allowlist
    assert "todo" in worker.tool_allowlist
    assert "mcp/*" in worker.tool_allowlist
    assert "background_task" not in worker.tool_allowlist
    assert "question" not in worker.tool_allowlist

    researcher = get_builtin_agent_manifest("researcher")
    assert researcher is not None
    assert "todo" not in researcher.tool_allowlist
    assert "background_task" not in researcher.tool_allowlist
    assert "question" not in researcher.tool_allowlist


def test_builtin_read_only_agent_tool_allowlists_exclude_mutating_capabilities() -> None:
    for preset in _READ_ONLY_AGENT_PRESETS:
        manifest = get_builtin_agent_manifest(preset)

        assert manifest is not None
        assert _MUTATING_TOOL_PATTERNS.isdisjoint(manifest.tool_allowlist)


def test_builtin_delegated_executor_roles_do_not_receive_recursive_task_tool() -> None:
    for preset in _CALLABLE_CHILD_AGENT_PRESETS:
        manifest = get_builtin_agent_manifest(preset)

        assert manifest is not None
        assert "task" not in manifest.tool_allowlist


def test_validate_builtin_agent_manifests_rejects_unknown_preset_hook_ref() -> None:
    with pytest.raises(ValueError, match="references unknown hook preset"):
        _ = validate_builtin_agent_manifests(
            (
                AgentManifest(
                    id="leader",
                    name="Leader",
                    mode="primary",
                    description="Primary preset",
                    prompt_profile="leader",
                    execution_engine="provider",
                    preset_hook_refs=("missing_hook",),
                    top_level_selectable=True,
                    prompt_materialization=AgentPromptMaterialization(profile="leader"),
                ),
            )
        )
