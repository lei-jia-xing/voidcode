from __future__ import annotations

import pytest

from voidcode.runtime.contracts import (
    RuntimeRequestError,
    validate_runtime_request_metadata,
)
from voidcode.runtime.permission import (
    DEFAULT_RUNTIME_EXECUTION_MODE,
    PLAN_MODE_DENIAL_REASON,
    PermissionPolicy,
    execution_mode_from_metadata,
    is_plan_mode_blocked,
    resolve_permission,
)
from voidcode.tools.contracts import ToolCall, ToolDefinition


def _read_only_tool() -> ToolDefinition:
    return ToolDefinition(
        name="grep",
        description="search read-only tool",
        input_schema={},
        read_only=True,
    )


def _write_tool() -> ToolDefinition:
    return ToolDefinition(
        name="write_file",
        description="mutating tool",
        input_schema={},
        read_only=False,
    )


def _call(name: str = "write_file") -> ToolCall:
    return ToolCall(tool_name=name, arguments={"path": "foo.txt"})


def test_default_runtime_execution_mode_is_act() -> None:
    assert DEFAULT_RUNTIME_EXECUTION_MODE == "act"


@pytest.mark.parametrize(
    ("mode", "tool", "operation_class", "expected"),
    [
        ("act", _write_tool(), "write", False),
        ("plan", _read_only_tool(), "read", False),
        ("plan", _read_only_tool(), None, False),
        ("plan", _write_tool(), "write", True),
        ("plan", _write_tool(), None, True),
        # Defense in depth: a read_only tool asked to perform a write/execute
        # operation is still denied while plan mode is active.
        ("plan", _read_only_tool(), "write", True),
        ("plan", _read_only_tool(), "execute", True),
    ],
)
def test_is_plan_mode_blocked_matrix(
    mode: str,
    tool: ToolDefinition,
    operation_class: str | None,
    expected: bool,
) -> None:
    assert (
        is_plan_mode_blocked(
            execution_mode=mode,  # type: ignore[arg-type]
            tool=tool,
            operation_class=operation_class,  # type: ignore[arg-type]
        )
        is expected
    )


def test_resolve_permission_plan_mode_denies_write_tool() -> None:
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        execution_mode="plan",
    )

    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_mode == "deny"
    assert outcome.pending_approval.policy_surface == "execution_mode.plan"
    assert outcome.pending_approval.reason == PLAN_MODE_DENIAL_REASON


def test_resolve_permission_plan_mode_allows_read_only_tool() -> None:
    outcome = resolve_permission(
        _read_only_tool(),
        _call("grep"),
        policy=PermissionPolicy(mode="ask"),
        execution_mode="plan",
    )

    assert outcome.decision == "allow"
    assert outcome.pending_approval is None


def test_resolve_permission_act_mode_does_not_short_circuit() -> None:
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        execution_mode="act",
    )

    assert outcome.decision == "ask"
    assert outcome.pending_approval is not None
    # Reason in act mode should remain the default approval reason rather than
    # the plan-mode denial sentinel.
    assert outcome.pending_approval.reason != PLAN_MODE_DENIAL_REASON


def test_resolve_permission_plan_mode_overrides_explicit_allow_rule() -> None:
    """Plan mode is a hard ceiling; even an `allow` rule cannot override it."""
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        rule_decision="allow",
        execution_mode="plan",
    )

    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "execution_mode.plan"


def test_execution_mode_from_metadata_defaults_to_act() -> None:
    assert execution_mode_from_metadata(None) == "act"
    assert execution_mode_from_metadata({}) == "act"
    assert execution_mode_from_metadata({"execution_mode": "act"}) == "act"
    # Invalid values fall back to default rather than raising; validation is
    # the responsibility of the runtime contract layer.
    assert execution_mode_from_metadata({"execution_mode": "weird"}) == "act"


def test_execution_mode_from_metadata_returns_plan_when_set() -> None:
    assert execution_mode_from_metadata({"execution_mode": "plan"}) == "plan"


def test_request_metadata_accepts_plan_execution_mode() -> None:
    normalized = validate_runtime_request_metadata({"execution_mode": "plan"})

    assert normalized.get("execution_mode") == "plan"


def test_request_metadata_accepts_act_execution_mode() -> None:
    normalized = validate_runtime_request_metadata({"execution_mode": "act"})

    assert normalized.get("execution_mode") == "act"


def test_request_metadata_rejects_unknown_execution_mode() -> None:
    with pytest.raises(RuntimeRequestError, match="execution_mode"):
        _ = validate_runtime_request_metadata({"execution_mode": "magic"})
