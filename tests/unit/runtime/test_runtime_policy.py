from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

_REQUIRED_SNAPSHOT_FIELDS = {
    "schema_version",
    "policy_version",
    "agent_preset",
    "agent_manifest_id",
    "intent",
    "tool_policy",
    "delegation_policy",
    "hook_policy",
    "prompt_activation",
    "precedence_trace",
    "diagnostics",
    "created_at",
    "mode",
    "read_only",
}


def _runtime_policy_module() -> Any:
    try:
        return importlib.import_module("voidcode.runtime.policy")
    except ModuleNotFoundError as exc:
        if exc.name == "voidcode.runtime.policy":
            pytest.fail("Runtime Harness Policy v1 module is not implemented yet")
        raise


def _sample_policy_inputs() -> dict[str, object]:
    return {
        "agent_preset": "leader",
        "agent_manifest_id": "leader",
        "runtime_config": {
            "tools": {"allowlist": ["read", "grep", "task"]},
            "policy": {"enabled": True, "version": "v1"},
        },
        "request_metadata": {},
        "parent_snapshot": None,
    }


def _materialize_sample_snapshot(**overrides: object) -> Mapping[str, object]:
    module = _runtime_policy_module()
    materialize_policy_snapshot = getattr(module, "materialize_runtime_policy_snapshot", None)
    if materialize_policy_snapshot is None:
        pytest.fail("materialize_runtime_policy_snapshot() is not implemented yet")
    inputs = _sample_policy_inputs()
    inputs.update(overrides)
    snapshot = materialize_policy_snapshot(**inputs)
    if hasattr(snapshot, "as_payload"):
        snapshot = snapshot.as_payload()
    assert isinstance(snapshot, Mapping)
    return cast(Mapping[str, object], snapshot)


def test_runtime_policy_snapshot_schema_contains_required_v1_fields() -> None:
    snapshot = _materialize_sample_snapshot()

    assert set(snapshot) >= _REQUIRED_SNAPSHOT_FIELDS
    assert ("created_at" in snapshot) ^ ("turn_id" in snapshot)
    assert snapshot["schema_version"] == 1
    assert isinstance(snapshot["policy_version"], str)
    assert snapshot["agent_preset"] == "leader"
    assert snapshot["agent_manifest_id"] == "leader"
    for field in (
        "intent",
        "tool_policy",
        "delegation_policy",
        "hook_policy",
        "prompt_activation",
        "diagnostics",
    ):
        assert isinstance(snapshot[field], Mapping), field
    assert isinstance(snapshot["precedence_trace"], Sequence)
    assert "metadata" not in snapshot


def test_runtime_policy_config_requires_explicit_current_version() -> None:
    module = _runtime_policy_module()

    with pytest.raises(ValueError, match=r"policy\.version must be 'v1'"):
        _ = module.validate_runtime_policy_config_payload(
            {"enabled": True},
            source="policy",
        )


def test_runtime_policy_precedence_trace_orders_authoritative_sources() -> None:
    snapshot = _materialize_sample_snapshot(
        runtime_config={
            "policy": {
                "enabled": True,
                "version": "v1",
                "delegation_policy": {"allow": ["worker"]},
            }
        },
        request_metadata={"delegation": {"subagent_type": "worker"}},
    )

    trace = cast(Sequence[Mapping[str, object]], snapshot["precedence_trace"])
    sources = [entry.get("source") for entry in trace]
    assert sources[:8] == [
        "runtime_hard_denials",
        "persisted_session_policy",
        "runtime_config",
        "agent_manifest",
        "request_session_options",
        "hook_preset_metadata",
        "intent_metadata",
        "runtime_defaults",
    ]
    assert trace[1].get("applied") is False
    assert trace[0].get("reason") is None


def test_runtime_policy_intent_stays_neutral_for_free_text_inputs() -> None:
    snapshots = (
        _materialize_sample_snapshot(user_message="   "),
        _materialize_sample_snapshot(user_message="/plan ship the fix"),
        _materialize_sample_snapshot(user_message="<system-reminder> continue later"),
        _materialize_sample_snapshot(
            user_message="implement the fix",
            request_metadata={
                "command": {
                    "name": "plan",
                    "source": "builtin",
                    "arguments": [],
                    "raw_arguments": "",
                    "original_prompt": "/plan",
                }
            },
        ),
    )

    for snapshot in snapshots:
        intent = cast(Mapping[str, object], snapshot["intent"])
        assert intent["label"] == "unspecified"
        assert intent["confidence"] == 0.0
        assert intent["matched_rule_ids"] == []
        assert intent.get("guidance_ids", []) == []
        assert intent.get("narrowing_hints", []) == []


def test_runtime_policy_neutral_intent_cannot_grant_capabilities() -> None:
    snapshot = _materialize_sample_snapshot(runtime_config={"tools": {"allowlist": ["read"]}})

    tool_policy = cast(Mapping[str, object], snapshot["tool_policy"])
    delegation_policy = cast(Mapping[str, object], snapshot["delegation_policy"])
    intent = cast(Mapping[str, object], snapshot["intent"])
    trace = cast(Sequence[Mapping[str, object]], snapshot["precedence_trace"])
    assert intent["label"] == "unspecified"
    assert intent["confidence"] == 0.0
    assert intent["matched_rule_ids"] == []
    assert "write_file" not in cast(Sequence[str], tool_policy.get("allowed", ()))
    assert "task" not in cast(Sequence[str], tool_policy.get("allowed", ()))
    assert "product" in cast(Sequence[str], delegation_policy.get("allowed_presets", ()))
    intent_trace = next(entry for entry in trace if entry.get("source") == "intent_metadata")
    assert intent_trace.get("applied") is False
    assert intent_trace.get("authoritative") is False
    assert intent_trace.get("label") == "unspecified"
    assert intent_trace.get("confidence") == 0.0
    assert intent_trace.get("matched_rule_ids") == []


def test_runtime_policy_hooks_are_non_authoritative() -> None:
    snapshot = _materialize_sample_snapshot(
        hook_policy_request={
            "event_scope": "pre_tool",
            "actions": ["observe", "report", "grant_tool", "create_child"],
            "tool_policy_patch": {"allow": ["shell_exec"]},
            "delegation_policy_patch": {"allow": ["product"]},
        },
        runtime_config={"tools": {"allowlist": ["read"]}},
    )

    hook_policy = cast(Mapping[str, object], snapshot["hook_policy"])
    tool_policy = cast(Mapping[str, object], snapshot["tool_policy"])
    delegation_policy = cast(Mapping[str, object], snapshot["delegation_policy"])
    assert set(cast(Sequence[str], hook_policy.get("actions", ()))) <= {
        "observe",
        "report",
        "cancel",
        "guidance",
    }
    assert "shell_exec" not in cast(Sequence[str], tool_policy.get("allowed", ()))
    assert "product" in cast(Sequence[str], delegation_policy.get("allowed_presets", ()))


def test_runtime_policy_product_is_an_allowed_child_preset() -> None:
    snapshot = _materialize_sample_snapshot(
        request_metadata={"delegation": {"mode": "background", "subagent_type": "product"}},
        runtime_config={
            "policy": {"delegation_policy": {"allow": ["advisor", "product"]}},
            "agents": {"planner": {"preset": "product"}},
        },
    )

    delegation_policy = cast(Mapping[str, object], snapshot["delegation_policy"])
    assert "product" in cast(Sequence[str], delegation_policy.get("allowed_presets", ()))
    denied = cast(Sequence[Mapping[str, object]], delegation_policy.get("denied", ()))
    assert denied == []


def test_runtime_policy_requires_persisted_snapshot() -> None:
    module = _runtime_policy_module()
    error_type = module.RuntimePolicySnapshotVersionError

    with pytest.raises(error_type, match="runtime_policy snapshot is required"):
        module.runtime_policy_snapshot_from_session_metadata({})


@pytest.mark.parametrize(
    "unsupported_snapshot",
    [
        {"schema_version": 999, "policy_version": "v1"},
        {"schema_version": 1, "policy_version": "v999"},
    ],
)
def test_runtime_policy_rejects_unsupported_explicit_snapshot_versions(
    unsupported_snapshot: dict[str, object],
) -> None:
    module = _runtime_policy_module()
    error_type = getattr(module, "RuntimePolicySnapshotVersionError", None)
    assert error_type is not None

    with pytest.raises(error_type, match="unsupported runtime_policy"):
        _materialize_sample_snapshot(persisted_session_policy=unsupported_snapshot)

    with pytest.raises(error_type, match="unsupported runtime_policy"):
        module.runtime_policy_snapshot_from_session_metadata({"runtime_policy": unsupported_snapshot})


def test_runtime_policy_rejects_current_version_with_incomplete_shape() -> None:
    module = _runtime_policy_module()
    error_type = getattr(module, "RuntimePolicySnapshotVersionError", None)
    assert error_type is not None

    with pytest.raises(error_type, match="missing required fields"):
        module.runtime_policy_snapshot_from_session_metadata({"runtime_policy": {"schema_version": 1, "policy_version": "v1"}})


def test_runtime_policy_child_snapshot_is_subset_of_parent_snapshot() -> None:
    parent = _materialize_sample_snapshot(
        runtime_config={
            "tools": {"allowlist": ["read"]},
            "policy": {
                "delegation_policy": {"allow": ["explore"]},
                "hook_policy": {
                    "allowed_event_scopes": ["pre_tool"],
                    "actions": ["observe"],
                },
            },
        }
    )

    child = _materialize_sample_snapshot(
        agent_preset="explore",
        agent_manifest_id="explore",
        runtime_config={
            "tools": {"allowlist": ["read", "write_file", "task"]},
            "policy": {
                "delegation_policy": {"allow": ["explore", "worker"]},
                "hook_policy": {
                    "allowed_event_scopes": ["pre_tool", "post_tool"],
                    "actions": ["observe", "report"],
                },
            },
        },
        parent_snapshot=parent,
    )

    tool_policy = cast(Mapping[str, object], child["tool_policy"])
    delegation_policy = cast(Mapping[str, object], child["delegation_policy"])
    hook_policy = cast(Mapping[str, object], child["hook_policy"])
    trace = cast(Sequence[Mapping[str, object]], child["precedence_trace"])

    assert tool_policy["allowed"] == ["read"]
    assert delegation_policy["allowed_presets"] == ["explore"]
    assert hook_policy["allowed_event_scopes"] == ["pre_tool"]
    assert hook_policy["actions"] == ["observe"]
    assert any(entry.get("source") == "parent_runtime_policy_snapshot" for entry in trace)


def test_runtime_policy_diagnostics_are_redacted_and_bounded() -> None:
    snapshot = _materialize_sample_snapshot(
        diagnostics_input={
            "prompt": "secret prompt body",
            "skill_body": "secret skill body",
            "OPENAI_API_KEY": "secret-key",
            "safe_reason": "tool denied",
        }
    )

    diagnostics = cast(Mapping[str, object], snapshot["diagnostics"])
    serialized = repr(diagnostics)
    assert "secret prompt body" not in serialized
    assert "secret skill body" not in serialized
    assert "secret-key" not in serialized
    assert "raw_prompt" not in diagnostics
    assert "raw_skill_body" not in diagnostics
