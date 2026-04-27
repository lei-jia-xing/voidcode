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
from voidcode.agent.models import AgentManifest, AgentPromptMaterialization


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
        routing_hints={"tier": "primary"},
        top_level_selectable=True,
        prompt_materialization=AgentPromptMaterialization(profile="leader"),
    )

    assert manifest.live_default_fields == (
        "prompt_profile",
        "execution_engine",
        "model_preference",
        "tool_allowlist",
        "skill_refs",
        "top_level_selectable",
        "prompt_materialization",
    )
    assert manifest.intent_fields == ("routing_hints",)
    assert manifest.field_semantic("prompt_profile") == "live_default"
    assert manifest.field_semantic("top_level_selectable") == "live_default"
    assert manifest.field_semantic("prompt_materialization") == "live_default"
    assert manifest.field_semantic("routing_hints") == "intent"


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
