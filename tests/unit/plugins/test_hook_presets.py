"""Unit tests for the hooks plugin presets ported from ``voidcode.hook.presets``."""

from __future__ import annotations

import pytest

from voidcode.plugins.hooks.presets import (
    HookPreset,
    ResolvedHookPresetSnapshot,
    get_builtin_hook_preset,
    hook_preset_snapshot_from_payload,
    is_builtin_hook_preset_ref,
    list_builtin_hook_presets,
    resolve_hook_preset_refs,
    validate_hook_preset_actions,
    validate_hook_preset_event_scopes,
    validate_hook_preset_refs,
)

ALL_REFS = (
    "role_reminder",
    "delegation_guard",
    "background_output_quality_guidance",
    "delegated_retry_guidance",
    "delegated_task_timing_guidance",
    "todo_continuation_guidance",
)

ROLE_REMINDER_GUIDANCE = (
    "Follow the active agent preset exactly: preserve its responsibility boundary, tool scope, and output obligations for this run."
)
DELEGATION_GUARD_GUIDANCE = (
    "Delegate only through runtime-owned task routing, respect supported child "
    "presets, and never bypass runtime tool, approval, or session governance."
)
TODO_CONTINUATION_GUIDANCE = (
    "For multi-step work, keep todos current, complete finished items immediately, and use remaining todos to resume the next concrete action."
)
GUIDANCE_PREFIX = "Advisory only; runtime policy and permissions remain authoritative."


def test_every_builtin_ref_resolves() -> None:
    for ref in ALL_REFS:
        preset = get_builtin_hook_preset(ref)
        assert preset is not None
        assert preset.ref == ref
        assert is_builtin_hook_preset_ref(ref)


def test_list_builtin_presets_is_ordered_and_exhaustive() -> None:
    presets = list_builtin_hook_presets()
    assert tuple(preset.ref for preset in presets) == ALL_REFS
    assert all(isinstance(preset, HookPreset) for preset in presets)


def test_preset_kinds_and_scopes() -> None:
    expected_kinds = {
        "role_reminder": "guidance",
        "delegation_guard": "guard",
        "background_output_quality_guidance": "guidance",
        "delegated_retry_guidance": "guard",
        "delegated_task_timing_guidance": "guidance",
        "todo_continuation_guidance": "continuation",
    }
    expected_scopes = {
        "role_reminder": ("runtime.request_received", "core.model_turn"),
        "delegation_guard": (
            "core.tool_request_created",
            "runtime.permission_resolved",
            "runtime.tool_started",
        ),
        "background_output_quality_guidance": (
            "runtime.background_task_completed",
            "runtime.background_task_failed",
            "runtime.background_task_cancelled",
            "runtime.background_task_interrupted",
            "runtime.background_task_result_read",
            "runtime.delegated_result_available",
        ),
        "delegated_retry_guidance": (
            "runtime.background_task_failed",
            "runtime.background_task_cancelled",
            "runtime.background_task_interrupted",
            "runtime.delegated_result_available",
        ),
        "delegated_task_timing_guidance": (
            "runtime.background_task_registered",
            "runtime.background_task_started",
            "runtime.background_task_completed",
            "runtime.background_task_failed",
            "runtime.background_task_cancelled",
            "runtime.background_task_interrupted",
            "runtime.delegated_result_available",
        ),
        "todo_continuation_guidance": (
            "runtime.todo_updated",
            "runtime.turn_progress",
        ),
    }
    expected_actions = {
        "role_reminder": ("guidance",),
        "delegation_guard": ("observe", "report", "cancel", "guidance"),
        "background_output_quality_guidance": ("observe", "report", "guidance"),
        "delegated_retry_guidance": ("observe", "report", "guidance"),
        "delegated_task_timing_guidance": ("observe", "report", "guidance"),
        "todo_continuation_guidance": ("observe", "report", "guidance"),
    }
    for ref in ALL_REFS:
        preset = get_builtin_hook_preset(ref)
        assert preset is not None
        assert preset.kind == expected_kinds[ref]
        assert preset.event_scopes == expected_scopes[ref]
        assert preset.allowed_actions == expected_actions[ref]


def test_guidance_strings_are_verbatim() -> None:
    role = get_builtin_hook_preset("role_reminder")
    guard = get_builtin_hook_preset("delegation_guard")
    todo = get_builtin_hook_preset("todo_continuation_guidance")
    assert role is not None and guard is not None and todo is not None
    assert role.guidance == ROLE_REMINDER_GUIDANCE
    assert guard.guidance == DELEGATION_GUARD_GUIDANCE
    assert todo.guidance == TODO_CONTINUATION_GUIDANCE


def test_payload_shape_is_stable_v1() -> None:
    snapshot = resolve_hook_preset_refs(("role_reminder", "delegation_guard"))
    payload = snapshot.to_payload()
    assert payload == {
        "refs": ["role_reminder", "delegation_guard"],
        "presets": [
            {
                "ref": "role_reminder",
                "kind": "guidance",
                "source": "builtin",
                "event_scopes": ["runtime.request_received", "core.model_turn"],
                "allowed_actions": ["guidance"],
                "guidance": ROLE_REMINDER_GUIDANCE,
            },
            {
                "ref": "delegation_guard",
                "kind": "guard",
                "source": "builtin",
                "event_scopes": [
                    "core.tool_request_created",
                    "runtime.permission_resolved",
                    "runtime.tool_started",
                ],
                "allowed_actions": ["observe", "report", "cancel", "guidance"],
                "guidance": DELEGATION_GUARD_GUIDANCE,
            },
        ],
        "source": "builtin",
        "version": 1,
    }
    # Wire shape uses JSON-style lists, not tuples.
    assert isinstance(payload["presets"], list)
    for entry in payload["presets"]:
        assert isinstance(entry["event_scopes"], list)
        assert isinstance(entry["allowed_actions"], list)


def test_resolve_dedupes_refs_preserving_first_seen_order() -> None:
    snapshot = resolve_hook_preset_refs(("delegation_guard", "role_reminder", "delegation_guard"))
    assert snapshot.refs == ("delegation_guard", "role_reminder")
    assert tuple(entry["ref"] for entry in snapshot.presets) == ("delegation_guard", "role_reminder")


def test_guidance_context_empty_when_no_presets() -> None:
    assert ResolvedHookPresetSnapshot(refs=(), presets=()).guidance_context() == ""


def test_guidance_context_prefix_and_inclusion() -> None:
    snapshot = resolve_hook_preset_refs(("role_reminder", "todo_continuation_guidance"))
    context = snapshot.guidance_context()
    assert context == (f"{GUIDANCE_PREFIX}\n- {ROLE_REMINDER_GUIDANCE}\n- {TODO_CONTINUATION_GUIDANCE}")
    assert context.startswith(GUIDANCE_PREFIX + "\n")
    assert DELEGATION_GUARD_GUIDANCE not in context


def test_unknown_ref_returns_none_from_lookup() -> None:
    assert get_builtin_hook_preset("not_a_preset") is None
    assert is_builtin_hook_preset_ref("not_a_preset") is False


def test_unknown_ref_fails_resolution_like_legacy() -> None:
    valid_refs = ", ".join(ALL_REFS)
    with pytest.raises(ValueError) as excinfo:
        resolve_hook_preset_refs(("role_reminder", "not_a_preset"))
    assert str(excinfo.value) == f"hook preset refs references unknown hook preset: not_a_preset; valid presets are: {valid_refs}"


def test_empty_ref_fails_resolution_like_legacy() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_hook_preset_refs(("   ",))
    assert str(excinfo.value) == "hook preset refs entries must be non-empty strings"


def test_validate_refs_message_uses_supplied_field_path() -> None:
    valid_refs = ", ".join(ALL_REFS)
    with pytest.raises(ValueError) as excinfo:
        validate_hook_preset_refs(("bogus",), field_path="hook.presets")
    assert str(excinfo.value) == f"hook.presets references unknown hook preset: bogus; valid presets are: {valid_refs}"


def test_empty_resolution_payload_and_context() -> None:
    snapshot = resolve_hook_preset_refs(())
    assert snapshot.refs == ()
    assert snapshot.presets == ()
    assert snapshot.to_payload() == {"refs": [], "presets": [], "source": "builtin", "version": 1}
    assert snapshot.guidance_context() == ""


def test_snapshot_round_trips_through_payload() -> None:
    snapshot = resolve_hook_preset_refs(ALL_REFS)
    restored = hook_preset_snapshot_from_payload(snapshot.to_payload())
    assert restored == snapshot
    assert hook_preset_snapshot_from_payload(None) is None
    assert hook_preset_snapshot_from_payload({"refs": []}) is None


def test_snapshot_payload_mismatch_is_rejected() -> None:
    payload = resolve_hook_preset_refs(("role_reminder",)).to_payload()
    payload["presets"][0]["guidance"] = "different"
    with pytest.raises(ValueError) as excinfo:
        hook_preset_snapshot_from_payload(payload)
    assert str(excinfo.value) == ("persisted hook preset snapshot presets[0].guidance does not match builtin hook preset: role_reminder")


def test_forbidden_and_invalid_actions_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_hook_preset_actions(("bypass_approval",), field_path="x")
    assert str(excinfo.value) == "x contains forbidden authority action: bypass_approval"

    with pytest.raises(ValueError) as excinfo:
        validate_hook_preset_actions(("nope",), field_path="x")
    assert str(excinfo.value) == ("x contains invalid action: nope; valid actions are: observe, report, cancel, guidance")


def test_invalid_event_scope_rejected() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_hook_preset_event_scopes(("runtime.nope",), field_path="x")
    assert str(excinfo.value).startswith("x contains invalid event scope: runtime.nope; valid scopes are: ")


def test_hook_preset_rejects_blank_fields() -> None:
    with pytest.raises(ValueError) as excinfo:
        HookPreset(
            ref="ref",
            kind="guidance",
            description="   ",
            guidance="g",
            event_scopes=("core.model_turn",),
        )
    assert str(excinfo.value) == "HookPreset.description must be a non-empty string"
