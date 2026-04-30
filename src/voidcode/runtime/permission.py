from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ..tools.contracts import ToolCall, ToolDefinition

type PermissionDecision = Literal["allow", "deny", "ask"]
type PermissionResolution = Literal["allow", "deny"]
type PathScope = Literal["workspace", "external"]
type OperationClass = Literal["read", "write", "execute"]


@dataclass(frozen=True, slots=True)
class PermissionOutcome:
    decision: PermissionDecision
    pending_approval: PendingApproval | None = None

    def __post_init__(self) -> None:
        if self.decision == "ask" and self.pending_approval is None:
            raise ValueError("ask decisions require a pending approval")


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    mode: PermissionDecision = "ask"


@dataclass(frozen=True, slots=True)
class ExternalDirectoryPolicy:
    rules: tuple[tuple[str, PermissionDecision], ...] = (("*", "ask"),)


@dataclass(frozen=True, slots=True)
class ExternalDirectoryPermissionConfig:
    read: ExternalDirectoryPolicy = field(default_factory=ExternalDirectoryPolicy)
    write: ExternalDirectoryPolicy = field(
        default_factory=lambda: ExternalDirectoryPolicy(rules=(("*", "deny"),))
    )


@dataclass(frozen=True, slots=True)
class DelegationGovernance:
    max_depth: int = 3
    spawn_budget: int = 4


@dataclass(frozen=True, slots=True)
class PendingApproval:
    request_id: str
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    target_summary: str = ""
    reason: str = ""
    policy_mode: PermissionDecision = "ask"
    request_event_sequence: int | None = None
    owner_session_id: str | None = None
    owner_parent_session_id: str | None = None
    delegated_task_id: str | None = None
    path_scope: PathScope | None = None
    operation_class: OperationClass | None = None
    canonical_path: str | None = None
    matched_rule: str | None = None
    policy_surface: str | None = None


def default_policy_for_tool(tool: ToolDefinition) -> PermissionPolicy:
    if tool.read_only:
        return PermissionPolicy(mode="allow")
    return PermissionPolicy(mode="ask")


def resolve_permission(
    tool: ToolDefinition,
    tool_call: ToolCall,
    *,
    policy: PermissionPolicy,
    owner_session_id: str | None = None,
    owner_parent_session_id: str | None = None,
    delegated_task_id: str | None = None,
    path_scope: PathScope = "workspace",
    operation_class: OperationClass | None = None,
    canonical_path: str | None = None,
    matched_rule: str | None = None,
    policy_surface: str | None = None,
    external_decision: PermissionDecision | None = None,
) -> PermissionOutcome:
    if path_scope == "workspace" and tool.read_only:
        return PermissionOutcome(decision="allow")

    if path_scope == "external" and external_decision is not None:
        pending_approval = build_pending_approval(
            tool_call,
            policy=PermissionPolicy(mode=external_decision),
            owner_session_id=owner_session_id,
            owner_parent_session_id=owner_parent_session_id,
            delegated_task_id=delegated_task_id,
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=canonical_path,
            matched_rule=matched_rule,
            policy_surface=policy_surface,
        )
        if external_decision == "ask":
            return PermissionOutcome(decision="ask", pending_approval=pending_approval)
        return PermissionOutcome(decision=external_decision, pending_approval=pending_approval)

    pending_approval = build_pending_approval(
        tool_call,
        policy=policy,
        owner_session_id=owner_session_id,
        owner_parent_session_id=owner_parent_session_id,
        delegated_task_id=delegated_task_id,
        path_scope=path_scope,
        operation_class=operation_class,
        canonical_path=canonical_path,
        matched_rule=matched_rule,
        policy_surface=policy_surface,
    )
    if policy.mode == "ask":
        return PermissionOutcome(decision="ask", pending_approval=pending_approval)
    return PermissionOutcome(decision=policy.mode, pending_approval=pending_approval)


def build_pending_approval(
    tool_call: ToolCall,
    *,
    policy: PermissionPolicy,
    owner_session_id: str | None = None,
    owner_parent_session_id: str | None = None,
    delegated_task_id: str | None = None,
    path_scope: PathScope | None = None,
    operation_class: OperationClass | None = None,
    canonical_path: str | None = None,
    matched_rule: str | None = None,
    policy_surface: str | None = None,
) -> PendingApproval:
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
        reason="non-read-only tool invocation",
        policy_mode=policy.mode,
        owner_session_id=owner_session_id,
        owner_parent_session_id=owner_parent_session_id,
        delegated_task_id=delegated_task_id,
        path_scope=path_scope,
        operation_class=operation_class,
        canonical_path=canonical_path,
        matched_rule=matched_rule,
        policy_surface=policy_surface,
    )


def evaluate_external_directory_policy(
    *,
    policy: ExternalDirectoryPolicy,
    canonical_path: Path,
) -> tuple[PermissionDecision, str]:
    normalized_path = canonical_path.as_posix()
    for pattern, decision in policy.rules:
        if _path_matches_rule(normalized_path=normalized_path, pattern=pattern):
            return decision, pattern
    return "ask", "*"


def _path_matches_rule(*, normalized_path: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    expanded_pattern = pattern
    if pattern.startswith("~"):
        try:
            expanded_pattern = Path(pattern).expanduser().as_posix()
        except RuntimeError:
            return False
    else:
        expanded_pattern = pattern.replace("\\", "/")

    if pattern == "*":
        return True
    return fnmatch(normalized_path, expanded_pattern)
