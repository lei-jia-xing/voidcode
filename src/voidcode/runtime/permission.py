from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from ..tools.contracts import ToolCall, ToolDefinition

type PermissionDecision = Literal["allow", "deny", "ask"]
type PermissionResolution = Literal["allow", "deny"]


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    mode: PermissionDecision = "allow"


@dataclass(frozen=True, slots=True)
class PendingApproval:
    request_id: str
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    target_summary: str = ""
    reason: str = ""
    policy_mode: PermissionDecision = "ask"


def default_policy_for_tool(tool: ToolDefinition) -> PermissionPolicy:
    if tool.read_only:
        return PermissionPolicy(mode="allow")
    return PermissionPolicy(mode="ask")


def build_pending_approval(tool_call: ToolCall, *, policy: PermissionPolicy) -> PendingApproval:
    path = tool_call.arguments.get("path")
    if isinstance(path, str) and path:
        target_summary = f"{tool_call.tool_name} {path}"
    else:
        target_summary = tool_call.tool_name
    return PendingApproval(
        request_id=f"approval-{uuid4()}",
        tool_name=tool_call.tool_name,
        arguments=dict(tool_call.arguments),
        target_summary=target_summary,
        reason="write-capable tool invocation",
        policy_mode=policy.mode,
    )
