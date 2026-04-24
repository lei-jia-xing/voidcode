from __future__ import annotations

import pytest

from voidcode.agent import (
    is_builtin_prompt_profile,
    list_builtin_agent_manifests,
    render_agent_prompt,
    render_builtin_prompt_profile,
)
from voidcode.agent import prompts as prompt_module
from voidcode.agent.builtin import validate_builtin_agent_manifests
from voidcode.agent.models import AgentManifest


def test_builtin_agent_manifests_have_materialized_prompt_profiles_and_execution_engines() -> None:
    manifests = list_builtin_agent_manifests()

    assert manifests
    for manifest in manifests:
        assert manifest.prompt_profile is not None
        assert manifest.execution_engine == "provider"
        prompt = render_builtin_prompt_profile(manifest.prompt_profile)
        assert prompt is not None
        assert prompt


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
    prompt_module._render_known_builtin_prompt_profile.cache_clear()

    assert render_builtin_prompt_profile("leader") is not None
    cache_info = prompt_module._render_known_builtin_prompt_profile.cache_info()
    assert cache_info.currsize == 1

    assert render_builtin_prompt_profile("custom-review") is None
    assert render_builtin_prompt_profile("another-custom-profile") is None

    cache_info = prompt_module._render_known_builtin_prompt_profile.cache_info()
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
    )

    assert manifest.live_default_fields == (
        "prompt_profile",
        "execution_engine",
        "model_preference",
        "tool_allowlist",
        "skill_refs",
    )
    assert manifest.intent_fields == ("routing_hints",)
    assert manifest.field_semantic("prompt_profile") == "live_default"
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
