from __future__ import annotations

import pytest

from voidcode.hook.presets import (
    get_builtin_hook_preset,
    hook_preset_snapshot_from_payload,
    is_builtin_hook_preset_ref,
    list_builtin_hook_presets,
    resolve_hook_preset_refs,
    validate_hook_preset_refs,
)


def test_builtin_hook_preset_catalog_contains_agent_manifest_refs() -> None:
    refs = tuple(preset.ref for preset in list_builtin_hook_presets())

    assert refs == (
        "role_reminder",
        "delegation_guard",
        "background_output_quality_guidance",
        "todo_continuation_guidance",
    )
    assert all(get_builtin_hook_preset(ref) is not None for ref in refs)


def test_builtin_hook_presets_carry_guidance_metadata() -> None:
    role_reminder = get_builtin_hook_preset("role_reminder")

    assert role_reminder is not None
    assert role_reminder.kind == "guidance"
    assert "role" in role_reminder.description.lower()
    assert "active agent preset" in role_reminder.guidance


def test_hook_preset_ref_helpers_reject_unknown_refs() -> None:
    assert is_builtin_hook_preset_ref("delegation_guard") is True
    assert is_builtin_hook_preset_ref("python") is False

    with pytest.raises(ValueError, match="references unknown hook preset: python"):
        _ = validate_hook_preset_refs(("python",), field_path="agent.hook_refs")


def test_resolved_hook_preset_snapshot_renders_guidance_context() -> None:
    snapshot = resolve_hook_preset_refs(("role_reminder", "role_reminder", "delegation_guard"))
    payload = snapshot.to_payload()
    restored = hook_preset_snapshot_from_payload(payload)

    assert payload["refs"] == ["role_reminder", "delegation_guard"]
    assert restored is not None
    context = restored.guidance_context()
    assert "Resolved agent hook preset guidance." in context
    assert "do not expand tool permissions" in context
    assert "active agent preset" in context
    assert "runtime-owned task routing" in context
