from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.contracts import (
    RuntimeRequestError,
    runtime_mode_from_metadata,
    runtime_read_only_from_metadata,
    validate_runtime_request_metadata,
)
from voidcode.runtime.permission import (
    PLAN_MODE_DENIAL_REASON,
    OperationClass,
    PermissionPolicy,
    default_policy_for_tool,
    is_plan_mode_blocked,
    resolve_permission,
)
from voidcode.runtime.permission_context import RuntimePermissionContextResolver, operation_class_for_tool
from voidcode.tools import AstGrepTool, ShellExecTool
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
        name="write",
        description="mutating tool",
        input_schema={},
        read_only=False,
    )


def _call(name: str = "write") -> ToolCall:
    return ToolCall(tool_name=name, arguments={"path": "foo.txt"})


@pytest.mark.parametrize(
    ("read_only", "tool", "operation_class", "expected"),
    [
        (False, _write_tool(), "write", False),
        (True, _read_only_tool(), "read", False),
        (True, _read_only_tool(), None, False),
        (True, _write_tool(), "write", True),
        (True, _write_tool(), None, True),
        # Defense in depth: a read_only tool asked to perform a write/execute
        # operation is still denied while the read-only stance is active.
        (True, _read_only_tool(), "write", True),
        (True, _read_only_tool(), "execute", True),
    ],
)
def test_is_plan_mode_blocked_matrix(
    read_only: bool,
    tool: ToolDefinition,
    operation_class: str | None,
    expected: bool,
) -> None:
    assert (
        is_plan_mode_blocked(
            read_only=read_only,
            tool=tool,
            operation_class=cast(OperationClass | None, operation_class),
        )
        is expected
    )


def test_resolve_permission_read_only_denies_write_tool() -> None:
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        read_only=True,
    )

    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_mode == "deny"
    assert outcome.pending_approval.policy_surface == "mode.plan"
    assert outcome.pending_approval.reason == PLAN_MODE_DENIAL_REASON


def test_resolve_permission_read_only_denies_shell_execute_operation() -> None:
    shell_tool = ShellExecTool()
    shell = shell_tool.definition
    call = ToolCall(tool_name="shell_exec", arguments={"command": "pwd"})
    operation = operation_class_for_tool(
        call.tool_name,
        shell.read_only,
        tool_instance=shell_tool,
        arguments=call.arguments,
    )
    outcome = resolve_permission(
        shell,
        call,
        policy=PermissionPolicy(mode="allow"),
        operation_class=operation,
        read_only=True,
    )

    assert operation == "execute"
    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "mode.plan"
    assert outcome.pending_approval.operation_class == "execute"


def test_resolve_permission_read_only_allows_read_only_tool() -> None:
    outcome = resolve_permission(
        _read_only_tool(),
        _call("grep"),
        policy=PermissionPolicy(mode="ask"),
        read_only=True,
    )

    assert outcome.decision == "allow"
    assert outcome.pending_approval is None


def test_resolve_permission_normal_mode_does_not_short_circuit() -> None:
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        read_only=False,
    )

    assert outcome.decision == "ask"
    assert outcome.pending_approval is not None
    # Reason in normal mode should remain the default approval reason rather than
    # the read-only denial sentinel.
    assert outcome.pending_approval.reason != PLAN_MODE_DENIAL_REASON


def test_resolve_permission_read_only_overrides_explicit_allow_rule() -> None:
    """The read-only stance is a hard ceiling; even an `allow` rule cannot override it."""
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        rule_decision="allow",
        read_only=True,
    )

    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "mode.plan"


def test_request_metadata_defaults_to_normal_action_capable_runtime_mode() -> None:
    normalized = validate_runtime_request_metadata({})

    assert "mode" not in normalized
    assert "read_only" not in normalized
    assert runtime_mode_from_metadata(normalized) == "normal"
    assert runtime_read_only_from_metadata(normalized) is False


@pytest.mark.parametrize("mode", ["normal", "plan"])
def test_request_metadata_accepts_runtime_mode(mode: str) -> None:
    normalized = validate_runtime_request_metadata({"mode": mode})

    assert normalized.get("mode") == mode


def test_request_metadata_rejects_unknown_runtime_mode() -> None:
    with pytest.raises(RuntimeRequestError, match="mode"):
        _ = validate_runtime_request_metadata({"mode": "magic"})


@pytest.mark.parametrize("read_only", [True, False])
def test_request_metadata_accepts_explicit_read_only_flag(read_only: bool) -> None:
    normalized = validate_runtime_request_metadata({"read_only": read_only})

    assert normalized.get("read_only") is read_only
    assert runtime_read_only_from_metadata(normalized) is read_only


def test_request_metadata_rejects_non_boolean_read_only_flag() -> None:
    with pytest.raises(RuntimeRequestError, match="read_only"):
        _ = validate_runtime_request_metadata({"read_only": "true"})


def test_plan_runtime_mode_implies_effective_read_only() -> None:
    normalized = validate_runtime_request_metadata({"mode": "plan", "read_only": False})

    assert runtime_mode_from_metadata(normalized) == "plan"
    assert runtime_read_only_from_metadata(normalized) is True


def test_normal_runtime_mode_allows_explicit_read_only() -> None:
    normalized = validate_runtime_request_metadata({"mode": "normal", "read_only": True})

    assert runtime_mode_from_metadata(normalized) == "normal"
    assert runtime_read_only_from_metadata(normalized) is True


def test_runtime_mode_helper_rejects_invalid_unvalidated_metadata() -> None:
    with pytest.raises(RuntimeRequestError, match="mode"):
        _ = runtime_mode_from_metadata({"mode": "magic"})


def test_runtime_read_only_helper_rejects_invalid_unvalidated_read_only() -> None:
    with pytest.raises(RuntimeRequestError, match="read_only"):
        _ = runtime_read_only_from_metadata({"read_only": "true"})


def test_default_policy_allows_read_only_and_asks_for_write() -> None:
    read_tool = _read_only_tool()
    write_tool = _write_tool()

    assert default_policy_for_tool(read_tool).mode == "allow"
    assert default_policy_for_tool(write_tool).mode == "ask"
    assert (
        resolve_permission(
            read_tool,
            _call("grep"),
            policy=default_policy_for_tool(read_tool),
        ).decision
        == "allow"
    )
    assert (
        resolve_permission(
            write_tool,
            _call(),
            policy=default_policy_for_tool(write_tool),
        ).decision
        == "ask"
    )


@pytest.mark.parametrize("mode", ["search", "preview"])
def test_ast_grep_non_replace_is_read_operation(mode: str) -> None:
    definition = ToolDefinition(name="ast_grep", description="AST search", read_only=True)
    operation = operation_class_for_tool(
        "ast_grep",
        definition.read_only,
        tool_instance=AstGrepTool(),
        arguments={"mode": mode},
    )
    assert operation == "read"


def test_ast_grep_replace_is_write_operation_and_denied_in_plan_mode(tmp_path: Path) -> None:
    definition = ToolDefinition(name="ast_grep", description="AST rewrite", read_only=True)
    call = ToolCall(
        tool_name="ast_grep",
        arguments={"mode": "replace", "path": "sample.py", "apply": True},
    )
    resolver = RuntimePermissionContextResolver(workspace=tmp_path)
    _scope, _path, operation, _candidates = resolver.permission_context_for_tool_call(
        tool=definition,
        tool_instance=AstGrepTool(),
        tool_call=call,
        patch_path_extractor=lambda _patch: (),
    )
    assert operation == "write"
    outcome = resolve_permission(
        definition,
        call,
        policy=default_policy_for_tool(definition),
        operation_class=operation,
        read_only=True,
    )
    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "mode.plan"
