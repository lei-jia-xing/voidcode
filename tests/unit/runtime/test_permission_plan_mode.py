from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.contracts import (
    RuntimeRequestError,
    validate_runtime_request_metadata,
)
from voidcode.runtime.mode import runtime_mode_from_metadata, runtime_read_only_from_metadata
from voidcode.runtime.permission import (
    DEFAULT_APPROVAL_MODE,
    PLAN_MODE_DENIAL_REASON,
    ExternalDirectoryPermissionConfig,
    ExternalDirectoryPolicy,
    OperationClass,
    PathScope,
    PermissionPolicy,
    approval_decision,
    is_read_only_blocked,
    resolve_permission,
)
from voidcode.runtime.permission_context import RuntimePermissionContextResolver, operation_class_for_tool
from voidcode.runtime.permission_engine import PermissionEngine
from voidcode.tools.ast_grep import AstGrepTool
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolEffect
from voidcode.tools.shell_exec import ShellExecTool


def _read_only_tool() -> ToolDefinition:
    return ToolDefinition(
        name="grep",
        description="search read-only tool",
        input_schema={},
        effects=frozenset({ToolEffect.READ}),
    )


def _write_tool() -> ToolDefinition:
    return ToolDefinition(
        name="write",
        description="mutating tool",
        input_schema={},
        effects=frozenset({ToolEffect.WRITE}),
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
def test_is_read_only_blocked_matrix(
    read_only: bool,
    tool: ToolDefinition,
    operation_class: str | None,
    expected: bool,
) -> None:
    assert (
        is_read_only_blocked(
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
        shell.effects,
        tool_instance=shell_tool,
        arguments=call.arguments,
    )
    outcome = resolve_permission(
        shell,
        call,
        policy=PermissionPolicy(mode="yolo"),
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

    assert runtime_mode_from_metadata(normalized) == "normal"
    assert runtime_read_only_from_metadata(normalized) is False


@pytest.mark.parametrize("mode", ["normal", "plan"])
def test_request_metadata_accepts_runtime_mode(mode: str) -> None:
    normalized = validate_runtime_request_metadata({"mode": mode})

    assert normalized.get("mode") == mode


@pytest.mark.parametrize(
    ("helper", "metadata", "match"),
    (
        pytest.param(validate_runtime_request_metadata, {"mode": "magic"}, "mode", id="validate-unknown-mode"),
        pytest.param(
            validate_runtime_request_metadata,
            {"read_only": "true"},
            "read_only",
            id="validate-non-boolean-read-only",
        ),
    ),
)
def test_request_metadata_rejects_invalid_mode_and_read_only(
    helper: Any,
    metadata: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(RuntimeRequestError, match=match):
        _ = helper(metadata)


@pytest.mark.parametrize("read_only", [True, False])
def test_request_metadata_accepts_explicit_read_only_flag(read_only: bool) -> None:
    normalized = validate_runtime_request_metadata({"read_only": read_only})

    assert normalized.get("read_only") is read_only
    assert runtime_read_only_from_metadata(normalized) is read_only


def test_plan_runtime_mode_implies_effective_read_only() -> None:
    normalized = validate_runtime_request_metadata({"mode": "plan", "read_only": False})

    assert runtime_mode_from_metadata(normalized) == "plan"
    assert runtime_read_only_from_metadata(normalized) is True


def test_normal_runtime_mode_allows_explicit_read_only() -> None:
    normalized = validate_runtime_request_metadata({"mode": "normal", "read_only": True})

    assert runtime_mode_from_metadata(normalized) == "normal"
    assert runtime_read_only_from_metadata(normalized) is True


def test_default_policy_auto_approves_every_tier() -> None:
    read_tool = _read_only_tool()
    write_tool = _write_tool()
    default_policy = PermissionPolicy()

    assert default_policy.mode == DEFAULT_APPROVAL_MODE
    assert approval_decision(mode=DEFAULT_APPROVAL_MODE, operation_class="read") == "allow"
    assert approval_decision(mode=DEFAULT_APPROVAL_MODE, operation_class="write") == "allow"
    assert (
        resolve_permission(
            read_tool,
            _call("grep"),
            policy=default_policy,
            operation_class="read",
        ).decision
        == "allow"
    )
    assert (
        resolve_permission(
            write_tool,
            _call(),
            policy=default_policy,
            operation_class="write",
        ).decision
        == "allow"
    )


@pytest.mark.parametrize("mode", ["search", "preview"])
def test_ast_grep_non_replace_is_read_operation(mode: str) -> None:
    definition = ToolDefinition(name="ast_grep", description="AST search", effects=frozenset({ToolEffect.WRITE}))
    operation = operation_class_for_tool(
        "ast_grep",
        definition.effects,
        tool_instance=AstGrepTool(),
        arguments={"mode": mode},
    )
    assert operation == "read"


def test_ast_grep_replace_is_write_operation_and_denied_in_plan_mode(tmp_path: Path) -> None:
    definition = ToolDefinition(name="ast_grep", description="AST rewrite", effects=frozenset({ToolEffect.WRITE}))
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
        policy=PermissionPolicy(),
        operation_class=operation,
        read_only=True,
    )
    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "mode.plan"


class _StubTool:
    pass


class _StubResolver:
    """A resolver double pinned to two external paths: one allowed, one denied."""

    def permission_context_for_tool_call(
        self,
        *,
        tool: ToolDefinition,
        tool_instance: object,
        tool_call: ToolCall,
        patch_path_extractor: object,
    ) -> tuple[PathScope, str | None, OperationClass, tuple[str, ...]]:
        _ = tool, tool_instance, tool_call, patch_path_extractor
        return ("external", "/ext-a/x", "read", ("/ext-a/x", "/ext-b/y"))

    def normalized_permission_path_candidates(
        self,
        tool_call: ToolCall,
        external_paths: tuple[str, ...],
        *,
        patch_path_extractor: object,
        tool: ToolDefinition | None = None,
    ) -> tuple[str, ...]:
        _ = tool_call, external_paths, patch_path_extractor, tool
        return ()


def test_external_directory_policy_denies_across_multiple_external_paths() -> None:
    """A deny on any external path wins over allows on the other paths."""
    definition = ToolDefinition(name="read", description="read", input_schema={}, effects=frozenset({ToolEffect.READ}))
    engine = PermissionEngine(
        _context_resolver=_StubResolver(),  # type: ignore[arg-type]
        _permission_config=ExternalDirectoryPermissionConfig(),
    )

    evaluation = engine.evaluate(
        tool=definition,
        tool_instance=_StubTool(),  # type: ignore[arg-type]
        tool_call=ToolCall(tool_name="read", arguments={"path": "/ext-a/x"}),
        permission_rules=(),
        permission_config=ExternalDirectoryPermissionConfig(
            read=ExternalDirectoryPolicy(rules=(("/ext-b/*", "deny"), ("*", "allow"))),
        ),
    )

    assert evaluation.external_decision == "deny"
    assert evaluation.matched_rule == "/ext-b/*"
