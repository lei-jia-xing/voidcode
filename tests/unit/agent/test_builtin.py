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
from voidcode.runtime.workflow import (
    WorkflowMcpBindingIntent,
    WorkflowPreset,
    list_builtin_workflow_presets,
    load_builtin_workflow_preset_registry,
    validate_workflow_presets,
    workflow_preset_from_payload,
    workflow_presets_from_payload,
)

_READ_ONLY_AGENT_PRESETS = ("advisor", "explore", "researcher", "product")
_DELEGATED_ONLY_AGENT_PRESETS = ("worker", "advisor", "explore", "researcher")
_CALLABLE_CHILD_AGENT_PRESETS = (*_DELEGATED_ONLY_AGENT_PRESETS, "product")
_MUTATING_TOOL_PATTERNS = frozenset(
    {
        "write_file",
        "edit",
        "multi_edit",
        "apply_patch",
        "shell_exec",
        "format_file",
        "ast_grep_replace",
        "task",
    }
)
_PROMPT_BOUNDARY_PHRASES = {
    "leader": (
        "primary user-facing runtime agent",
        "You own the final user-facing outcome",
        "Child agents provide bounded assistance",
        "Route work to the right specialist, supervise execution, verify results yourself",
        "Runtime tool allowlists, approval checks, and background task state remain authoritative",
    ),
    "worker": (
        "focused delegated executor",
        "You execute, not orchestrate",
        "Do not redelegate",
        "Finish the assigned scope yourself",
        "Worker is a focused executor, not an orchestrator",
    ),
    "advisor": (
        "read-heavy preset for architecture, risk, and review guidance",
        "Stay read only and advisory",
        "do not edit or write files",
        "Do not run mutating tools",
    ),
    "explore": (
        "workspace-bound preset for local code discovery",
        "Address the caller's actual need",
        "caller can proceed without another discovery round",
        "Use absolute paths for every file reference",
        "Stay read only",
        "do not edit or write files",
        "Do not mutate the workspace",
    ),
    "researcher": (
        "public docs, code examples, and external references",
        "Stay read only and non-mutating",
        "do not edit files",
        "Do not claim implementation ownership",
        "Distinguish official documentation, source examples, and incidental commentary",
    ),
    "product": (
        "top-level planning preset",
        "You are not an executor; you are a planning partner",
        "Do not write, edit, or execute code",
        "Do not claim code execution, implementation ownership, or verification of changes",
        "without re-discovering the problem statement, non-goals, or definition of done",
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
    assert "background_retry" in prompt
    assert "inspect the returned retry task id with background_output" in prompt
    assert "do not manually reconstruct child requests" in prompt


def test_leader_prompt_distinguishes_product_from_delegated_worker_roles() -> None:
    prompt = render_builtin_prompt_profile("leader")

    assert prompt is not None
    assert "Use category when you know the kind of work" in prompt
    assert "Use subagent_type when you already know the specialist you need" in prompt
    assert "Use product when the next best move is product thinking" in prompt
    assert "not an implementation worker" in prompt
    assert "or product for planning" not in prompt


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
        "product",
    ]
    assert is_agent_top_level_selectable("leader") is True
    assert is_agent_top_level_selectable("product") is True
    assert is_agent_top_level_selectable("worker") is False
    assert is_agent_top_level_selectable("advisor") is False
    assert is_agent_top_level_selectable("explore") is False
    assert is_agent_top_level_selectable("researcher") is False
    assert is_agent_top_level_selectable("missing") is False
    assert tuple(manifest.id for manifest in list_top_level_selectable_agent_manifests()) == (
        "leader",
        "product",
    )


def test_builtin_top_level_selectability_matches_runtime_executable_presets() -> None:
    top_level_manifest_ids = {
        manifest.id for manifest in list_builtin_agent_manifests() if manifest.top_level_selectable
    }
    executable_agent_presets = cast(
        frozenset[str],
        vars(runtime_service_module)["_EXECUTABLE_AGENT_PRESETS"],
    )

    assert top_level_manifest_ids == {"leader", "product"}
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


def test_builtin_subagent_tool_allowlists_enforce_role_boundaries() -> None:
    write_tools = {"write_file", "edit", "multi_edit", "apply_patch"}

    for preset in ("advisor", "explore"):
        manifest = get_builtin_agent_manifest(preset)
        assert manifest is not None
        assert write_tools.isdisjoint(manifest.tool_allowlist)
        assert "task" not in manifest.tool_allowlist

    worker = get_builtin_agent_manifest("worker")
    assert worker is not None
    assert write_tools.issubset(worker.tool_allowlist)
    assert "task" not in worker.tool_allowlist


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


def test_builtin_retry_tool_is_leader_only_runtime_recovery_surface() -> None:
    leader = get_builtin_agent_manifest("leader")
    assert leader is not None
    assert "background_retry" in leader.tool_allowlist

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
        assert "read only" in prompt.lower()
        assert "do not edit" in prompt.lower()


def test_worker_prompt_and_manifest_forbid_redelegation() -> None:
    manifest = get_builtin_agent_manifest("worker")
    prompt = render_agent_prompt({"preset": "worker", "prompt_profile": "worker"})

    assert manifest is not None
    assert prompt is not None
    assert manifest.mode == "subagent"
    assert manifest.top_level_selectable is False
    assert "task" not in manifest.tool_allowlist
    assert "Do not redelegate" in prompt
    assert "Do not call task or create child agents" in prompt
    assert "not an orchestrator" in prompt


def test_product_prompt_and_manifest_remain_planning_only() -> None:
    manifest = get_builtin_agent_manifest("product")
    prompt = render_agent_prompt({"preset": "product", "prompt_profile": "product"})

    assert manifest is not None
    assert prompt is not None
    assert manifest.mode == "primary"
    assert manifest.top_level_selectable is True
    assert _MUTATING_TOOL_PATTERNS.isdisjoint(manifest.tool_allowlist)
    assert "planning preset" in prompt
    assert "not an executor" in prompt
    assert "Do not claim code execution" in prompt


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


def test_builtin_workflow_presets_cover_mvp_registry_foundation() -> None:
    registry = load_builtin_workflow_preset_registry()
    expected_ids = (
        "research",
        "implementation",
        "frontend",
        "review",
        "git",
    )

    assert tuple(preset.id for preset in list_builtin_workflow_presets()) == expected_ids
    assert tuple(preset.id for preset in registry.list_presets()) == expected_ids
    assert set(registry.presets) == set(expected_ids)
    research = registry.get("research")
    implementation = registry.get("implementation")
    frontend = registry.get("frontend")
    review = registry.get("review")
    git = registry.get("git")
    assert research is not None
    assert implementation is not None
    assert frontend is not None
    assert review is not None
    assert git is not None
    assert research.default_agent == "researcher"
    assert research.read_only_default is True
    assert implementation.default_agent == "leader"
    assert frontend.default_agent == "leader"
    assert review.default_agent == "advisor"
    assert review.read_only_default is True
    assert implementation.read_only_default is False
    assert frontend.read_only_default is False
    assert git.category == "git"
    assert git.read_only_default is False


def test_builtin_workflow_presets_expose_issue_405_named_capability_intents() -> None:
    registry = load_builtin_workflow_preset_registry()

    git = registry.get("git")
    frontend = registry.get("frontend")
    research = registry.get("research")
    review = registry.get("review")
    assert git is not None
    assert frontend is not None
    assert research is not None
    assert review is not None

    assert "git-master" in git.skill_refs
    assert "frontend-design" in frontend.skill_refs
    assert "review-work" in review.skill_refs
    assert _workflow_mcp_servers(frontend) == {"playwright"}
    assert _workflow_mcp_servers(research) == {"context7", "websearch", "grep_app"}
    assert _workflow_mcp_servers(review) == {"context7", "websearch", "grep_app"}
    assert all(binding.required is False for binding in frontend.mcp_binding_intents)
    assert all(binding.required is False for binding in research.mcp_binding_intents)
    assert all(binding.required is False for binding in review.mcp_binding_intents)


def test_builtin_git_workflow_preset_declares_strict_safety_guidance() -> None:
    git = load_builtin_workflow_preset_registry().get("git")

    assert git is not None
    assert git.default_agent == "leader"
    assert git.tool_policy_ref is None
    assert git.permission_policy_ref == "runtime_default"
    assert git.read_only_default is False
    git_guidance = f"{git.prompt_append} {git.verification_guidance}".lower()
    assert "status" in git_guidance
    assert "diff" in git_guidance
    assert "runtime approval" in git_guidance
    assert "generic" in git_guidance
    assert "approval" in git_guidance
    assert "preserve hooks" in git_guidance
    assert "auto-approval" not in git_guidance


def _workflow_mcp_servers(preset: WorkflowPreset) -> set[str]:
    return {server for binding in preset.mcp_binding_intents for server in binding.servers}


def test_builtin_workflow_presets_validate_with_empty_capability_catalogs() -> None:
    presets = validate_workflow_presets(list_builtin_workflow_presets())

    assert tuple(preset.id for preset in presets) == (
        "research",
        "implementation",
        "frontend",
        "review",
        "git",
    )
    workflow_by_id = {preset.id: preset for preset in presets}
    assert workflow_by_id["research"].hook_preset_refs == (
        "role_reminder",
        "delegated_task_timing_guidance",
        "background_output_quality_guidance",
    )
    assert workflow_by_id["implementation"].hook_preset_refs == (
        "role_reminder",
        "delegated_task_timing_guidance",
        "delegated_retry_guidance",
        "todo_continuation_guidance",
    )
    assert workflow_by_id["frontend"].hook_preset_refs == (
        "role_reminder",
        "delegated_task_timing_guidance",
        "delegated_retry_guidance",
        "todo_continuation_guidance",
    )


def test_workflow_preset_payload_parser_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unsupported key"):
        _ = workflow_preset_from_payload(
            {
                "id": "custom",
                "default_agent": "leader",
                "category": "implementation",
                "unexpected": True,
            },
            field_path="workflows.custom",
        )


def test_workflow_preset_payload_parser_requires_id() -> None:
    with pytest.raises(ValueError, match="id is required"):
        _ = workflow_preset_from_payload(
            {"default_agent": "leader", "category": "implementation"},
            field_path="workflows.custom",
        )


def test_validate_workflow_presets_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate workflow preset id"):
        _ = validate_workflow_presets(
            (
                WorkflowPreset(id="custom", default_agent="leader", category="implementation"),
                WorkflowPreset(id="custom", default_agent="advisor", category="review"),
            )
        )


def test_validate_workflow_presets_rejects_missing_force_loaded_skill() -> None:
    with pytest.raises(ValueError, match="force_load_skills references missing skill"):
        _ = validate_workflow_presets(
            (
                WorkflowPreset(
                    id="custom",
                    default_agent="leader",
                    category="implementation",
                    force_load_skills=("missing-skill",),
                ),
            ),
            available_skill_names=("git-master",),
        )


def test_validate_workflow_presets_requires_configured_required_mcp_binding() -> None:
    with pytest.raises(ValueError, match="required mcp_binding_intents server is missing"):
        _ = validate_workflow_presets(
            (
                WorkflowPreset(
                    id="custom",
                    default_agent="leader",
                    category="implementation",
                    mcp_binding_intents=(WorkflowMcpBindingIntent(servers=("docs",)),),
                ),
            ),
            available_mcp_servers=(),
        )


def test_validate_workflow_presets_allows_optional_missing_mcp_binding() -> None:
    presets = validate_workflow_presets(
        (
            WorkflowPreset(
                id="custom",
                default_agent="leader",
                category="implementation",
                mcp_binding_intents=(
                    WorkflowMcpBindingIntent(profile="docs", servers=("context7",), required=False),
                ),
            ),
        ),
        available_mcp_profiles=(),
        available_mcp_servers=(),
    )

    assert presets[0].mcp_binding_intents[0].required is False


def test_workflow_presets_from_payload_rejects_map_key_id_mismatch() -> None:
    with pytest.raises(ValueError, match="id must match workflow preset map key"):
        _ = workflow_presets_from_payload(
            {
                "custom": {
                    "id": "other",
                    "default_agent": "leader",
                    "category": "implementation",
                }
            },
            available_skill_names=(),
        )


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
def test_render_agent_prompt_materializes_builtin_profiles(
    preset: str, expected_fragment: str
) -> None:
    prompt = render_agent_prompt({"preset": preset, "prompt_profile": preset})

    assert prompt is not None
    assert expected_fragment in prompt


def test_leader_prompt_requires_native_tool_actions_for_implementation() -> None:
    prompt = render_agent_prompt({"preset": "leader", "prompt_profile": "leader"})

    assert prompt is not None
    assert "act through the runtime's native tool calls" in prompt
    assert "writing code in prose is not doing the work" in prompt
    assert "If concrete action is required and suitable tools are available" in prompt
    assert "Never describe a tool call, patch, command, or file change as text" in prompt


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
