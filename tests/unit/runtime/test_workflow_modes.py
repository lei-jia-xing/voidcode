from __future__ import annotations

import pytest

from voidcode.runtime.workflow import (
    WorkflowMode,
    WorkflowModeResolution,
    get_builtin_workflow_mode,
    list_builtin_workflow_modes,
    resolve_workflow_mode,
)
from voidcode.runtime.workflow_snapshot import (
    workflow_snapshot_from_metadata,
    workflow_snapshot_selected_preset,
)


def test_builtin_workflow_modes_define_public_catalog_without_legacy_presets() -> None:
    expected_ids = ("default", "deep_work", "review", "product", "sustain")

    modes = list_builtin_workflow_modes()

    assert tuple(mode.id for mode in modes) == expected_ids
    assert all(isinstance(mode, WorkflowMode) for mode in modes)
    assert get_builtin_workflow_mode("default") == modes[0]
    assert get_builtin_workflow_mode("ultrawork") is None


def test_resolve_workflow_mode_defaults_to_default_mode() -> None:
    resolution = resolve_workflow_mode()

    assert isinstance(resolution, WorkflowModeResolution)
    assert resolution.mode.id == "default"
    assert resolution.source == "default"
    assert resolution.workflow_mode == "default"
    assert resolution.workflow_preset is None


def test_resolve_workflow_mode_uses_total_precedence() -> None:
    assert (
        resolve_workflow_mode(
            command_workflow_mode="product",
            metadata_workflow_mode="deep_work",
            workflow_preset="research",
        ).mode.id
        == "product"
    )
    assert (
        resolve_workflow_mode(
            metadata_workflow_mode="deep_work",
            workflow_preset="research",
        ).mode.id
        == "deep_work"
    )
    assert resolve_workflow_mode(workflow_preset="implementation").mode.id == "sustain"
    assert resolve_workflow_mode(workflow_preset="research").mode.id == "deep_work"
    assert resolve_workflow_mode(workflow_preset="review").mode.id == "review"


def test_resolve_workflow_mode_accepts_legacy_frontend_and_git_presets() -> None:
    assert resolve_workflow_mode(workflow_preset="frontend").mode.id == "product"
    assert resolve_workflow_mode(workflow_preset="git").mode.id == "sustain"
    assert get_builtin_workflow_mode("frontend") is None
    assert get_builtin_workflow_mode("git") is None


def test_builtin_workflow_modes_define_exact_hook_preset_bundles() -> None:
    modes = {mode.id: mode for mode in list_builtin_workflow_modes()}

    assert modes["default"].hook_preset_refs == ()
    assert modes["deep_work"].hook_preset_refs == (
        "role_reminder",
        "delegated_task_timing_guidance",
        "background_output_quality_guidance",
    )
    assert modes["review"].hook_preset_refs == ("role_reminder",)
    assert modes["product"].hook_preset_refs == ("role_reminder",)
    assert modes["sustain"].hook_preset_refs == (
        "role_reminder",
        "todo_continuation_guidance",
        "delegated_task_timing_guidance",
        "delegated_retry_guidance",
    )


def test_workflow_mode_rejects_unknown_hook_preset_refs() -> None:
    with pytest.raises(
        ValueError,
        match="workflow mode 'deep_work' hook_preset_refs.*missing_hook",
    ):
        _ = WorkflowMode(
            id="deep_work",
            description="Invalid deep work mode.",
            hook_preset_refs=("role_reminder", "missing_hook"),
        )


def test_resolve_workflow_mode_rejects_unknown_workflow_mode() -> None:
    with pytest.raises(ValueError, match="unknown workflow_mode.*banana"):
        _ = resolve_workflow_mode(metadata_workflow_mode="banana")


def test_resolve_workflow_mode_rejects_conflicting_mode_and_preset_values() -> None:
    with pytest.raises(
        ValueError,
        match="workflow_mode.*workflow_preset.*deep_work.*implementation",
    ):
        _ = resolve_workflow_mode(
            metadata_workflow_mode="deep_work",
            workflow_preset="implementation",
        )


def test_resolve_workflow_mode_allows_matching_mode_and_preset_values() -> None:
    resolution = resolve_workflow_mode(
        metadata_workflow_mode="deep_work",
        workflow_preset="research",
    )

    assert resolution.mode.id == "deep_work"
    assert resolution.source == "workflow_mode"
    assert resolution.workflow_mode == "deep_work"
    assert resolution.workflow_preset == "research"


def test_workflow_snapshot_from_metadata_prefers_stored_effective_material() -> None:
    metadata: dict[str, object] = {
        "workflow": {
            "snapshot_version": 2,
            "requested": {"workflow_mode": "deep_work", "workflow_preset": "research"},
            "effective": {
                "mode": "deep_work",
                "legacy_preset": "research",
                "source": "workflow_mode",
                "category": "saved-research",
                "default_agent": "researcher",
                "effective_agent": "researcher",
                "read_only_default": True,
                "prompt_append": "Use the saved prompt.",
                "hook_preset_refs": ["role_reminder"],
                "skill_refs": ["review-work"],
                "force_load_skills": ["git-master"],
                "mcp_binding_intents": [{"servers": ["context7"], "required": False}],
                "verification_guidance": "Use saved verification guidance.",
            },
        }
    }

    snapshot = workflow_snapshot_from_metadata(metadata)

    assert snapshot is not None
    assert snapshot["snapshot_version"] == 2
    assert snapshot["requested"] == {
        "workflow_mode": "deep_work",
        "workflow_preset": "research",
    }
    assert snapshot["effective"] == {
        "mode": "deep_work",
        "legacy_preset": "research",
        "source": "workflow_mode",
        "category": "saved-research",
        "default_agent": "researcher",
        "effective_agent": "researcher",
        "read_only_default": True,
        "prompt_append": "Use the saved prompt.",
        "hook_preset_refs": ["role_reminder"],
        "skill_refs": ["review-work"],
        "force_load_skills": ["git-master"],
        "mcp_binding_intents": [{"servers": ["context7"], "required": False}],
        "verification_guidance": "Use saved verification guidance.",
    }
    assert snapshot["category"] == "saved-research"
    assert snapshot["selected_preset"] == "research"
    assert workflow_snapshot_selected_preset(snapshot) == "research"


def test_workflow_snapshot_from_metadata_tolerates_legacy_shapes() -> None:
    legacy_snapshot = {
        "snapshot_version": 1,
        "selected_preset": "review",
        "preset_source": "builtin",
        "category": "review",
        "default_agent": "advisor",
        "read_only_default": True,
        "prompt_append": "Review only.",
        "hook_preset_refs": ["role_reminder"],
        "skill_refs": ["review-work"],
        "force_load_skills": [],
        "mcp_binding_intents": [],
        "verification_guidance": "Report findings.",
    }
    metadata_variants: tuple[dict[str, object], ...] = (
        {"workflow": legacy_snapshot},
        {"runtime_config": {"workflow": legacy_snapshot}},
        {"agent_capability_snapshot": {"workflow": legacy_snapshot}},
    )

    for metadata in metadata_variants:
        snapshot = workflow_snapshot_from_metadata(metadata)

        assert snapshot is not None
        assert snapshot["requested"] == {"workflow_mode": None, "workflow_preset": "review"}
        assert snapshot["effective"] == {
            "mode": None,
            "legacy_preset": "review",
            "source": "builtin",
            "category": "review",
            "default_agent": "advisor",
            "read_only_default": True,
            "prompt_append": "Review only.",
            "hook_preset_refs": ["role_reminder"],
            "skill_refs": ["review-work"],
            "force_load_skills": [],
            "mcp_binding_intents": [],
            "verification_guidance": "Report findings.",
        }
        assert workflow_snapshot_selected_preset(snapshot) == "review"
