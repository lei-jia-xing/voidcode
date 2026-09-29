"""Approval mode × tool tier: the real resolver's decision matrix.

The mode vocabulary is ``always-ask`` / ``write`` / ``yolo`` and the tier axis is
``operation_class_for_tool``: a read tier is auto-approved by every mode, a write
tier needs at least ``write``, and an execute tier needs ``yolo``. The matrix is
driven through the real :func:`resolve_permission` with real tool instances, so
the expectation table below is the *expected* side of a behavioral assertion, not
a constant compared to itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permission import (
    APPROVAL_MODES,
    ApprovalMode,
    OperationClass,
    PermissionPolicy,
    resolve_permission,
)
from voidcode.runtime.permission_context import operation_class_for_tool
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.tools.contracts import ToolCall, ToolDefinition
from voidcode.tools.glob import GlobTool
from voidcode.tools.mcp import McpTool
from voidcode.tools.shell_exec import ShellExecTool
from voidcode.tools.write import WriteTool

from .test_todo_reminder import _ScriptedGraph, _Step

#: The omp matrix: mode → tier → decision. This is the expected side; the test
#: drives the real resolver and compares against it.
_EXPECTED_DECISION: dict[ApprovalMode, dict[OperationClass, str]] = {
    "always-ask": {"read": "allow", "write": "ask", "execute": "ask"},
    "write": {"read": "allow", "write": "allow", "execute": "ask"},
    "yolo": {"read": "allow", "write": "allow", "execute": "allow"},
}

#: One real (tool, call) per tier, so the tier comes from production code.
_TIER_TOOLS: dict[OperationClass, tuple[ToolDefinition, ToolCall]] = {
    "read": (GlobTool().definition, ToolCall(tool_name="glob", arguments={"pattern": "*"})),
    "write": (WriteTool().definition, ToolCall(tool_name="write", arguments={"path": "out.txt", "content": "x"})),
    "execute": (ShellExecTool().definition, ToolCall(tool_name="shell_exec", arguments={"command": "ls"})),
}


@dataclass(slots=True)
class _TierTool:
    """A tool instance whose classification is the generic (non-MCP) path."""


def _instance_for(tier: OperationClass) -> Any:
    if tier == "read":
        return GlobTool()
    if tier == "write":
        return WriteTool()
    return ShellExecTool()


@pytest.mark.parametrize("mode", APPROVAL_MODES)
@pytest.mark.parametrize("tier", ["read", "write", "execute"])
def test_mode_by_tier_matrix_drives_the_real_resolver(mode: ApprovalMode, tier: OperationClass) -> None:
    definition, call = _TIER_TOOLS[tier]
    operation_class = operation_class_for_tool(
        call.tool_name,
        definition.read_only,
        tool_instance=_instance_for(tier),
        arguments=call.arguments,
    )
    assert operation_class == tier

    outcome = resolve_permission(
        definition,
        call,
        policy=PermissionPolicy(mode=mode),
        operation_class=operation_class,
    )

    assert outcome.decision == _EXPECTED_DECISION[mode][tier]
    # The resolver's read shortcut returns with no audit payload; every other
    # path builds one. The decision is the observable that changes per mode.
    if tier == "read":
        assert outcome.pending_approval is None
    else:
        assert outcome.pending_approval is not None
        assert outcome.pending_approval.policy_mode == outcome.decision


def test_default_mode_prompts_for_write_tier_through_the_real_path(tmp_path: Path) -> None:
    """The unset-config default is observed behaviorally: a write-tier call prompts.

    Neither the mode nor the permission policy is passed here, so the runtime's
    own default selection is what resolves the call.
    """
    graph = _ScriptedGraph([_write_step(), _Step(output="done", is_finished=True)])
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([WriteTool()]),
        graph=graph,
        config=RuntimeConfig(execution_engine="deterministic"),
    )
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id="approval-default-mode")))

    events = [chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None]
    approval_requested = [event for event in events if event.event_type == "runtime.approval_requested"]

    assert len(approval_requested) == 1
    assert approval_requested[0].payload["tool"] == "write"
    assert approval_requested[0].payload["operation_class"] == "write"


def test_unknown_tool_defaults_to_execute_tier() -> None:
    """A tool with no declaration and no read-only flag is the safe ``execute`` default."""
    undeclared = _TierTool()

    assert operation_class_for_tool("custom_manifest_tool", False, tool_instance=undeclared) == "execute"
    assert operation_class_for_tool("custom_manifest_tool", True, tool_instance=undeclared) == "read"


def test_mcp_tools_declare_write_tier() -> None:
    """MCP server tools are ``write``: they mutate server state without running code."""
    from voidcode.mcp.types import McpToolSafety

    def _mcp(server: str, tool: str, *, read_only: bool) -> McpTool:
        return McpTool(
            server_name=server,
            tool_name=tool,
            description="MCP tool",
            input_schema={"type": "object"},
            safety=McpToolSafety(read_only=read_only),
            requester=cast(Any, object()),
        )

    mutating = _mcp("demo", "mutate", read_only=False)
    inspecting = _mcp("demo", "inspect", read_only=True)

    assert operation_class_for_tool("mcp/demo/mutate", mutating.definition.read_only, tool_instance=mutating) == "write"
    assert operation_class_for_tool("mcp/demo/inspect", inspecting.definition.read_only, tool_instance=inspecting) == "read"


def test_read_only_denial_still_wins_over_yolo_resolution() -> None:
    """Plan/read-only denial is above the mode matrix; ``yolo`` cannot re-enable it."""
    definition, call = _TIER_TOOLS["write"]
    outcome = resolve_permission(
        definition,
        call,
        policy=PermissionPolicy(mode="yolo"),
        operation_class="write",
        read_only=True,
    )

    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "mode.plan"


def _write_step() -> _Step:
    return _Step(tool_call=ToolCall(tool_name="write", arguments={"path": "auto.txt", "content": "written"}))


def _exec_step() -> _Step:
    return _Step(tool_call=ToolCall(tool_name="shell_exec", arguments={"command": "echo hi"}))


@pytest.mark.parametrize(
    ("mode", "step", "tool_name", "expect_approval"),
    [
        ("write", _write_step, "write", False),
        ("write", _exec_step, "shell_exec", True),
    ],
)
def test_write_mode_auto_approves_write_tier_but_prompts_for_execute(
    tmp_path: Path,
    mode: ApprovalMode,
    step: Any,
    tool_name: str,
    expect_approval: bool,
) -> None:
    """End-to-end: under mode ``write`` the write-tier call resolves with no
    pending approval (``runtime.approval_resolved`` with ``allow``), while the
    execute-tier call parks on a ``runtime.approval_requested``."""
    graph = _ScriptedGraph([step(), _Step(output="done", is_finished=True)])
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=None,
        tool_registry=ToolRegistry.from_tools([WriteTool(), ShellExecTool()]),
        graph=graph,
        config=RuntimeConfig(
            execution_engine="deterministic",
            approval_mode=mode,
        ),
        permission_policy=PermissionPolicy(mode=mode),
    )
    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=f"approval-tier-{mode}-{tool_name}")))
    events = [chunk.event for chunk in chunks if chunk.kind == "event" and chunk.event is not None]

    approval_requested = [event for event in events if event.event_type == "runtime.approval_requested"]
    approval_resolved = [event for event in events if event.event_type == "runtime.approval_resolved"]

    if expect_approval:
        assert len(approval_requested) == 1
        assert approval_requested[0].payload["tool"] == tool_name
        assert approval_requested[0].payload["decision"] == "ask"
        assert approval_requested[0].payload["operation_class"] == "execute"
        assert approval_resolved == []
    else:
        assert approval_requested == []
        assert [event.payload["decision"] for event in approval_resolved] == ["allow"]
        assert [event.payload["operation_class"] for event in approval_resolved] == ["write"]
