from __future__ import annotations

from typing import Protocol, cast

import pytest

from voidcode.agent import (
    get_builtin_agent_manifest,
    is_agent_top_level_selectable,
    is_builtin_prompt_profile,
    list_builtin_agent_manifests,
    list_top_level_selectable_agent_manifests,
    render_agent_prompt,
    render_builtin_prompt_profile,
    select_prompt_profile_for_manifest,
)
from voidcode.agent import prompts as prompt_module
from voidcode.agent.builtin import validate_builtin_agent_manifests
from voidcode.agent.models import AgentManifest, AgentMcpBindingIntent, AgentPromptMaterialization
from voidcode.hook.presets import is_builtin_hook_preset_ref, list_builtin_hook_presets
from voidcode.runtime import service as runtime_service_module
from voidcode.runtime.task import SubagentRoutingIdentity, resolve_subagent_route

_READ_ONLY_AGENT_PRESETS = ("advisor", "explore", "researcher", "product")
_DELEGATED_ONLY_AGENT_PRESETS = ("worker", "advisor", "explore", "researcher", "product")
_CALLABLE_CHILD_AGENT_PRESETS = _DELEGATED_ONLY_AGENT_PRESETS
_MUTATING_TOOL_PATTERNS = frozenset(
    {
        "write_file",
        "edit",
        "multi_edit",
        "apply_patch",
        "shell_exec",
        "ast_grep_replace",
        "task",
    }
)
_PROMPT_BOUNDARY_PHRASES = {
    "leader": (
        "primary user-facing runtime agent",
        "Deliver complete working behavior",
        "Verify child results yourself",
        "narrowest specialist that fits",
        "Collect outstanding child results with background_output",
    ),
    "worker": (
        "focused delegated executor",
        "You execute; you do not orchestrate",
        "do not delegate or spawn child agents",
    ),
    "advisor": (
        "read-only advisor for architecture, risk, and review",
        "Stay read-only",
        "do not edit or write files",
    ),
    "explore": (
        "workspace-bound agent for local code discovery",
        "identify the actual information need",
        "report relevant files with absolute paths",
        "Stay read-only",
        "do not edit or write files",
    ),
    "researcher": (
        "public docs, code examples, and external references",
        "Stay read-only and non-mutating",
        "do not edit files",
        "so the caller can make a concrete decision",
        "distinguish official documentation, source examples, and incidental commentary",
    ),
    "product": (
        "produce a concrete, ready-to-execute implementation plan",
        "Do not ask the user questions or wait for clarification",
        "state assumptions explicitly and plan around them",
        "do not write, edit, or execute code",
        "items to verify during implementation",
    ),
}


class _PromptCacheInfo(Protocol):
    @property
    def currsize(self) -> int: ...


class _CachedPromptRenderer(Protocol):
    def __call__(self, prompt_profile: str) -> str | None: ...

    def cache_clear(self) -> None: ...

    def cache_info(self) -> _PromptCacheInfo: ...


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


def test_leader_prompt_guides_runtime_owned_background_retry() -> None:
    prompt = render_builtin_prompt_profile("leader")

    assert prompt is not None
    assert "background_retry" not in prompt
    assert "Delegate only through the runtime's task tool" in prompt
    assert "Collect outstanding child results with background_output" in prompt
    assert "track the full set until every member is terminal" in prompt


def test_leader_prompt_lists_product_as_a_delegable_child_specialist() -> None:
    prompt = render_builtin_prompt_profile("leader")

    assert prompt is not None
    assert "narrowest specialist that fits" in prompt
    assert "explore, advisor, worker, researcher, product" in prompt
    assert "delegate to the product agent" in prompt
    assert "read its plan back via submit_result" in prompt
    assert "top-level planning preset" not in prompt


def test_builtin_agent_prompt_materialization_versions_match_prompt_contracts() -> None:
    expected_versions = {
        "leader": 2,
        "worker": 2,
        "advisor": 2,
        "explore": 2,
        "researcher": 2,
        "product": 2,
    }

    for manifest in list_builtin_agent_manifests():
        assert manifest.prompt_materialization is not None
        assert manifest.prompt_materialization.version == expected_versions[manifest.id]


def test_builtin_agent_manifests_declare_top_level_selectability() -> None:
    manifests = list_builtin_agent_manifests()

    assert [manifest.id for manifest in manifests if manifest.top_level_selectable] == [
        "leader",
    ]
    assert is_agent_top_level_selectable("leader") is True
    assert is_agent_top_level_selectable("product") is False
    assert is_agent_top_level_selectable("worker") is False
    assert is_agent_top_level_selectable("advisor") is False
    assert is_agent_top_level_selectable("explore") is False
    assert is_agent_top_level_selectable("researcher") is False
    assert is_agent_top_level_selectable("missing") is False
    assert tuple(manifest.id for manifest in list_top_level_selectable_agent_manifests()) == ("leader",)


def test_builtin_top_level_selectability_matches_runtime_executable_presets() -> None:
    top_level_manifest_ids = {manifest.id for manifest in list_builtin_agent_manifests() if manifest.top_level_selectable}
    executable_agent_presets = cast(
        frozenset[str],
        vars(runtime_service_module)["_EXECUTABLE_AGENT_PRESETS"],
    )

    assert top_level_manifest_ids == {"leader"}
    assert top_level_manifest_ids == executable_agent_presets


def test_builtin_delegated_only_agent_manifests_are_not_top_level_selectable() -> None:
    for preset in _DELEGATED_ONLY_AGENT_PRESETS:
        manifest = get_builtin_agent_manifest(preset)

        assert manifest is not None
        assert manifest.mode == "subagent"
        assert manifest.top_level_selectable is False
        assert is_agent_top_level_selectable(preset) is False


def test_builtin_callable_child_presets_align_with_runtime_delegation_routes() -> None:
    executable_subagent_presets = cast(
        frozenset[str],
        vars(runtime_service_module)["_EXECUTABLE_SUBAGENT_PRESETS"],
    )

    assert executable_subagent_presets == set(_CALLABLE_CHILD_AGENT_PRESETS)
    for preset in _CALLABLE_CHILD_AGENT_PRESETS:
        route = resolve_subagent_route(SubagentRoutingIdentity(mode="sync", subagent_type=preset))

        assert route.selected_preset == preset

    with pytest.raises(ValueError, match="leader.*not a callable child preset"):
        _ = resolve_subagent_route(SubagentRoutingIdentity(mode="sync", subagent_type="leader"))


def test_builtin_manifests_omit_removed_memory_tools() -> None:
    leader = get_builtin_agent_manifest("leader")
    product = get_builtin_agent_manifest("product")
    memory_tools = {"memory_add", "memory_delete", "memory_list", "memory_search"}

    assert leader is not None
    assert memory_tools.isdisjoint(leader.tool_allowlist)
    assert product is not None
    assert memory_tools.isdisjoint(product.tool_allowlist)


def test_builtin_subagent_tool_allowlists_enforce_role_boundaries() -> None:
    write_tools = {"write_file", "edit", "multi_edit", "apply_patch"}

    for preset in ("advisor", "explore"):
        manifest = get_builtin_agent_manifest(preset)
        assert manifest is not None
        assert write_tools.isdisjoint(manifest.tool_allowlist)
        assert "task" not in manifest.tool_allowlist
        assert "background_output" not in manifest.tool_allowlist
        assert "question" not in manifest.tool_allowlist

    worker = get_builtin_agent_manifest("worker")
    assert worker is not None
    assert write_tools.issubset(worker.tool_allowlist)
    assert "task" not in worker.tool_allowlist
    assert "todo_write" in worker.tool_allowlist
    assert "mcp/*" in worker.tool_allowlist
    assert "background_output" not in worker.tool_allowlist
    assert "question" not in worker.tool_allowlist

    researcher = get_builtin_agent_manifest("researcher")
    assert researcher is not None
    assert "todo_write" not in researcher.tool_allowlist
    assert "background_output" not in researcher.tool_allowlist
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


def test_builtin_leader_recovery_surface_omits_removed_retry_tool() -> None:
    leader = get_builtin_agent_manifest("leader")
    assert leader is not None
    assert "background_retry" not in leader.tool_allowlist
    assert "todo_write" in leader.tool_allowlist
    assert "background_output" in leader.tool_allowlist
    assert "question" in leader.tool_allowlist

    for preset in _CALLABLE_CHILD_AGENT_PRESETS:
        manifest = get_builtin_agent_manifest(preset)

        assert manifest is not None
        assert "background_retry" not in manifest.tool_allowlist


def test_builtin_read_only_role_prompts_and_manifests_align() -> None:
    for preset in ("advisor", "explore", "researcher"):
        manifest = get_builtin_agent_manifest(preset)
        prompt = render_agent_prompt({"preset": preset, "prompt_profile": preset})

        assert manifest is not None
        assert prompt is not None
        assert manifest.mode == "subagent"
        assert manifest.top_level_selectable is False
        assert _MUTATING_TOOL_PATTERNS.isdisjoint(manifest.tool_allowlist)
        assert "read-only" in prompt.lower()
        assert "do not edit" in prompt.lower()


def test_worker_prompt_and_manifest_forbid_redelegation() -> None:
    manifest = get_builtin_agent_manifest("worker")
    prompt = render_agent_prompt({"preset": "worker", "prompt_profile": "worker"})

    assert manifest is not None
    assert prompt is not None
    assert manifest.mode == "subagent"
    assert manifest.top_level_selectable is False
    assert "task" not in manifest.tool_allowlist
    assert "do not delegate or spawn child agents" in prompt
    assert "you do not orchestrate" in prompt


def test_product_prompt_and_manifest_form_a_non_interactive_planning_agent() -> None:
    manifest = get_builtin_agent_manifest("product")
    prompt = render_agent_prompt({"preset": "product", "prompt_profile": "product"})

    assert manifest is not None
    assert prompt is not None
    assert manifest.mode == "subagent"
    assert manifest.top_level_selectable is False
    assert _MUTATING_TOOL_PATTERNS.isdisjoint(manifest.tool_allowlist)
    assert "question" not in manifest.tool_allowlist
    assert "todo_write" not in manifest.tool_allowlist
    assert "task" not in manifest.tool_allowlist
    assert "submit_result" in manifest.tool_allowlist
    assert "background_output" not in manifest.tool_allowlist
    assert "without user interaction" in manifest.description
    assert "product agent" in prompt
    assert "Do not ask the user questions or wait for clarification" in prompt
    assert "do not write, edit, or execute code" in prompt
    assert "items to verify during implementation" in prompt
    assert "call submit_result" in prompt


def test_leader_prompt_balances_low_filler_output_with_complete_delivery() -> None:
    prompt = render_agent_prompt({"preset": "leader", "prompt_profile": "leader"})

    assert prompt is not None
    assert "Deliver complete working behavior" in prompt
    assert "report what you changed and how you verified it" in prompt
    assert "continue while actionable in-scope work remains" in prompt
    assert "gather evidence before claiming anything" in prompt
    assert "Make the smallest correct change or give the direct answer" not in prompt
    assert "Keep default answers short unless the user asks for detail" not in prompt


@pytest.mark.parametrize(
    ("preset", "required_phrases"),
    tuple(_PROMPT_BOUNDARY_PHRASES.items()),
)
def test_builtin_role_prompts_keep_critical_boundary_contracts(
    preset: str,
    required_phrases: tuple[str, ...],
) -> None:
    prompt = render_agent_prompt({"preset": preset, "prompt_profile": preset})

    assert prompt is not None
    for phrase in required_phrases:
        assert phrase in prompt


def test_builtin_agent_preset_hook_refs_resolve_through_hook_catalog() -> None:
    catalog_refs = {preset.ref for preset in list_builtin_hook_presets()}

    assert catalog_refs
    for manifest in list_builtin_agent_manifests():
        assert manifest.preset_hook_refs
        assert set(manifest.preset_hook_refs) <= catalog_refs
        for hook_ref in manifest.preset_hook_refs:
            assert is_builtin_hook_preset_ref(hook_ref) is True


def test_builtin_agent_skill_refs_follow_explicit_catalog_lazy_policy() -> None:
    for manifest in list_builtin_agent_manifests():
        assert manifest.skill_refs == ()


def test_prompt_profile_selection_uses_materialization_fallback() -> None:
    manifest = get_builtin_agent_manifest("leader")

    assert manifest is not None
    assert select_prompt_profile_for_manifest(manifest) == "leader"
    assert select_prompt_profile_for_manifest(manifest, model_family="unknown") == "leader"


def test_prompt_profile_selection_supports_model_family_overrides() -> None:
    manifest = AgentManifest(
        id="leader",
        name="Leader",
        mode="primary",
        description="Primary preset",
        prompt_profile="leader",
        execution_engine="provider",
        prompt_materialization=AgentPromptMaterialization(
            profile="leader",
            model_family_overrides={"compact": "worker"},
        ),
    )

    assert select_prompt_profile_for_manifest(manifest, model_family="compact") == "worker"
    assert select_prompt_profile_for_manifest(manifest, model_family="unknown") == "leader"


def test_render_agent_prompt_uses_model_family_materialization_override() -> None:
    prompt = render_agent_prompt(
        {
            "preset": "leader",
            "prompt_profile": "leader",
            "prompt_materialization": AgentPromptMaterialization(
                profile="leader",
                model_family_overrides={"compact": "worker"},
            ),
        },
        model_family="compact",
    )

    assert prompt is not None
    assert "VoidCode's worker agent" in prompt


@pytest.mark.parametrize(
    ("preset", "expected_fragment"),
    [
        ("leader", "VoidCode's leader agent"),
        ("worker", "VoidCode's worker agent"),
        ("advisor", "VoidCode's advisor agent"),
        ("explore", "VoidCode's explore agent"),
        ("researcher", "VoidCode's researcher agent"),
        ("product", "VoidCode's product agent"),
    ],
)
def test_render_agent_prompt_materializes_builtin_profiles(preset: str, expected_fragment: str) -> None:
    prompt = render_agent_prompt({"preset": preset, "prompt_profile": preset})

    assert prompt is not None
    assert expected_fragment in prompt


def test_leader_prompt_requires_native_tool_actions_for_implementation() -> None:
    prompt = render_agent_prompt({"preset": "leader", "prompt_profile": "leader"})

    assert prompt is not None
    assert "Act through the runtime's tools" in prompt
    assert "gather evidence before claiming anything" in prompt
    assert "Never present an unrun command, unread file, or unverified change as done" in prompt
    assert "report what you changed and how you verified it" in prompt


def test_render_agent_prompt_falls_back_for_non_builtin_profiles() -> None:
    prompt = render_agent_prompt({"preset": "leader", "prompt_profile": "custom-review"})

    assert prompt == (
        "Runtime-selected VoidCode agent prompt profile: custom-review. "
        "Treat this as the active agent role profile for this single-agent turn while "
        "still following the runtime-provided tool and skill boundaries."
    )


def test_builtin_prompt_lookup_rejects_non_builtin_profile_before_file_access() -> None:
    assert is_builtin_prompt_profile("leader") is True
    assert is_builtin_prompt_profile("custom-review") is False
    assert is_builtin_prompt_profile("../leader") is False
    assert render_builtin_prompt_profile("../leader") is None


def test_non_builtin_prompt_profiles_do_not_grow_builtin_prompt_cache() -> None:
    renderer_name = "_render_known_builtin_prompt_profile"
    renderer = cast(
        _CachedPromptRenderer,
        getattr(prompt_module, renderer_name),
    )
    renderer.cache_clear()

    assert render_builtin_prompt_profile("leader") is not None
    cache_info = renderer.cache_info()
    assert cache_info.currsize == 1

    assert render_builtin_prompt_profile("custom-review") is None
    assert render_builtin_prompt_profile("another-custom-profile") is None

    cache_info = renderer.cache_info()
    assert cache_info.currsize == 1


def test_agent_manifest_exposes_live_default_vs_intent_field_semantics() -> None:
    manifest = AgentManifest(
        id="leader",
        name="Leader",
        mode="primary",
        description="Primary preset",
        prompt_profile="leader",
        execution_engine="provider",
        model_preference="opencode/gpt-5.4",
        tool_allowlist=("read_file",),
        skill_refs=("demo",),
        preset_hook_refs=("role_reminder",),
        mcp_binding=AgentMcpBindingIntent(servers=("docs",)),
        routing_hints={"tier": "primary"},
        top_level_selectable=True,
        prompt_materialization=AgentPromptMaterialization(profile="leader"),
    )

    assert manifest.live_default_fields == (
        "prompt_profile",
        "execution_engine",
        "model_preference",
        "tool_allowlist",
        "preset_hook_refs",
        "mcp_binding",
        "top_level_selectable",
        "prompt_materialization",
    )
    assert manifest.intent_fields == ("routing_hints",)
    assert manifest.field_semantic("prompt_profile") == "live_default"
    assert manifest.field_semantic("top_level_selectable") == "live_default"
    assert manifest.field_semantic("prompt_materialization") == "live_default"
    assert manifest.field_semantic("mcp_binding") == "live_default"
    assert manifest.field_semantic("routing_hints") == "intent"


def test_builtin_agent_manifests_use_explicit_preset_hook_refs_not_formatter_refs() -> None:
    leader = get_builtin_agent_manifest("leader")
    worker = get_builtin_agent_manifest("worker")

    assert leader is not None
    assert worker is not None
    assert "delegation_guard" in leader.preset_hook_refs
    assert "background_output_quality_guidance" in leader.preset_hook_refs
    assert "delegated_retry_guidance" in leader.preset_hook_refs
    assert "todo_continuation_guidance" in leader.preset_hook_refs
    assert worker.preset_hook_refs == ("role_reminder", "delegation_guard")


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


def test_validate_builtin_agent_manifests_rejects_duplicate_mcp_server_refs() -> None:
    with pytest.raises(ValueError, match="must not contain duplicates"):
        _ = AgentMcpBindingIntent(servers=("docs", "docs"))


def test_validate_builtin_agent_manifests_accepts_mcp_binding_intent() -> None:
    manifests = validate_builtin_agent_manifests(
        (
            AgentManifest(
                id="leader",
                name="Leader",
                mode="primary",
                description="Primary preset",
                prompt_profile="leader",
                execution_engine="provider",
                mcp_binding=AgentMcpBindingIntent(profile="docs", servers=("context7",)),
                top_level_selectable=True,
                prompt_materialization=AgentPromptMaterialization(profile="leader"),
            ),
        )
    )

    assert manifests[0].mcp_binding == AgentMcpBindingIntent(
        profile="docs",
        servers=("context7",),
    )


def test_validate_builtin_agent_manifests_rejects_unknown_prompt_profile() -> None:
    with pytest.raises(ValueError, match="references unknown prompt profile"):
        _ = validate_builtin_agent_manifests(
            (
                AgentManifest(
                    id="leader",
                    name="Leader",
                    mode="primary",
                    description="Primary preset",
                    prompt_profile="missing-profile",
                    execution_engine="provider",
                ),
            )
        )


def test_validate_builtin_agent_manifests_rejects_top_level_subagent() -> None:
    with pytest.raises(ValueError, match="subagent.*top_level_selectable"):
        _ = validate_builtin_agent_manifests(
            (
                AgentManifest(
                    id="worker",
                    name="Worker",
                    mode="subagent",
                    description="Worker preset",
                    prompt_profile="worker",
                    execution_engine="provider",
                    top_level_selectable=True,
                    prompt_materialization=AgentPromptMaterialization(profile="worker"),
                ),
            )
        )


def test_validate_builtin_agent_manifests_rejects_unknown_materialized_profile() -> None:
    with pytest.raises(ValueError, match="prompt_materialization.profile"):
        _ = validate_builtin_agent_manifests(
            (
                AgentManifest(
                    id="leader",
                    name="Leader",
                    mode="primary",
                    description="Primary preset",
                    prompt_profile="leader",
                    execution_engine="provider",
                    top_level_selectable=True,
                    prompt_materialization=AgentPromptMaterialization(profile="missing-profile"),
                ),
            )
        )


def test_validate_builtin_agent_manifests_rejects_unknown_model_family_override() -> None:
    with pytest.raises(ValueError, match="model_family_overrides"):
        _ = validate_builtin_agent_manifests(
            (
                AgentManifest(
                    id="leader",
                    name="Leader",
                    mode="primary",
                    description="Primary preset",
                    prompt_profile="leader",
                    execution_engine="provider",
                    top_level_selectable=True,
                    prompt_materialization=AgentPromptMaterialization(
                        profile="leader",
                        model_family_overrides={"unknown": "missing-profile"},
                    ),
                ),
            )
        )


def test_product_manifest_excludes_all_delegation_helpers() -> None:
    product = get_builtin_agent_manifest("product")

    assert product is not None
    assert product.top_level_selectable is False
    assert product.mode == "subagent"
    assert {
        "task",
        "background_output",
        "background_retry",
        "background_cancel",
    }.isdisjoint(product.tool_allowlist)


def test_product_resolves_as_a_delegable_child_preset() -> None:
    route = resolve_subagent_route(SubagentRoutingIdentity(mode="background", subagent_type="product"))

    assert route.selected_preset == "product"
