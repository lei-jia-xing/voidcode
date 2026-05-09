from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ..tools.contracts import ToolCall, ToolDefinition

type PermissionDecision = Literal["allow", "deny", "ask"]
type PermissionResolution = Literal["allow", "deny"]
type PathScope = Literal["workspace", "external"]
type OperationClass = Literal["read", "write", "execute"]
type RuntimeExecutionMode = Literal["plan", "act"]

DEFAULT_RUNTIME_EXECUTION_MODE: RuntimeExecutionMode = "act"
PLAN_MODE_DENIAL_REASON = "plan mode is active; mutating tools are denied"


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
    rules: tuple[tuple[str, PermissionDecision], ...] = (("*", "allow"),)


@dataclass(frozen=True, slots=True)
class PatternPermissionRule:
    decision: PermissionDecision
    tool: str = "*"
    path: str | None = None
    command: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalDirectoryPermissionConfig:
    read: ExternalDirectoryPolicy = field(default_factory=ExternalDirectoryPolicy)
    write: ExternalDirectoryPolicy = field(
        default_factory=lambda: ExternalDirectoryPolicy(rules=(("*", "allow"),))
    )
    rules: tuple[PatternPermissionRule, ...] = ()


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


def is_plan_mode_blocked(
    *,
    execution_mode: RuntimeExecutionMode,
    tool: ToolDefinition,
    operation_class: OperationClass | None = None,
) -> bool:
    """Return True when plan mode must deny a tool call.

    Plan mode is a read-only execution stance: every non-read-only tool is
    denied regardless of approval policy or path scope, and any explicit
    write/execute operation class is denied even if the tool itself is
    advertised as read-only (defense in depth).
    """
    if execution_mode != "plan":
        return False
    if not tool.read_only:
        return True
    return operation_class in ("write", "execute")


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
    rule_decision: PermissionDecision | None = None,
    execution_mode: RuntimeExecutionMode = DEFAULT_RUNTIME_EXECUTION_MODE,
) -> PermissionOutcome:
    if is_plan_mode_blocked(
        execution_mode=execution_mode, tool=tool, operation_class=operation_class
    ):
        pending_approval = build_pending_approval(
            tool_call,
            policy=PermissionPolicy(mode="deny"),
            owner_session_id=owner_session_id,
            owner_parent_session_id=owner_parent_session_id,
            delegated_task_id=delegated_task_id,
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=canonical_path,
            matched_rule=matched_rule,
            policy_surface="execution_mode.plan",
            reason=PLAN_MODE_DENIAL_REASON,
        )
        return PermissionOutcome(decision="deny", pending_approval=pending_approval)
    if rule_decision is not None:
        pending_approval = build_pending_approval(
            tool_call,
            policy=PermissionPolicy(mode=rule_decision),
            owner_session_id=owner_session_id,
            owner_parent_session_id=owner_parent_session_id,
            delegated_task_id=delegated_task_id,
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=canonical_path,
            matched_rule=matched_rule,
            policy_surface=policy_surface,
        )
        if rule_decision == "ask":
            return PermissionOutcome(decision="ask", pending_approval=pending_approval)
        return PermissionOutcome(decision=rule_decision, pending_approval=pending_approval)

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
    reason: str = "non-read-only tool invocation",
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
        reason=reason,
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


def evaluate_pattern_permission_rules(
    *,
    rules: tuple[PatternPermissionRule, ...],
    tool_name: str,
    path_candidates: tuple[str, ...] = (),
    command: str | None = None,
) -> tuple[PermissionDecision, str] | None:
    for index, rule in enumerate(rules):
        if not _tool_matches_rule(tool_name=tool_name, pattern=rule.tool):
            continue
        if rule.command is not None and not _command_matches_rule(
            command=command, pattern=rule.command
        ):
            continue
        if rule.path is not None and not _path_candidates_match_rule(
            path_candidates=path_candidates,
            pattern=rule.path,
        ):
            continue
        return rule.decision, _format_pattern_permission_rule(index=index, rule=rule)
    return None


def _tool_matches_rule(*, tool_name: str, pattern: str) -> bool:
    return fnmatchcase(tool_name, pattern)


def _command_matches_rule(*, command: str | None, pattern: str) -> bool:
    if command is None:
        return False
    if fnmatchcase(command, pattern):
        return True
    for candidate in _command_match_candidates(command):
        if fnmatchcase(candidate, pattern):
            return True
    return False


def _command_match_candidates(command: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for segment in re.split(r"[;&|]+", command):
        stripped = segment.strip()
        if not stripped:
            continue
        candidates.append(stripped)
        first_token = stripped.split(None, 1)[0].strip()
        if first_token:
            candidates.append(first_token)
    return tuple(dict.fromkeys(candidates))


def _path_candidates_match_rule(*, path_candidates: tuple[str, ...], pattern: str) -> bool:
    if not path_candidates:
        return False
    return any(
        _path_matches_rule(normalized_path=path, pattern=pattern) for path in path_candidates
    )


def _format_pattern_permission_rule(*, index: int, rule: PatternPermissionRule) -> str:
    parts = [f"permission.rules[{index}]", f"tool={rule.tool!r}"]
    if rule.path is not None:
        parts.append(f"path={rule.path!r}")
    if rule.command is not None:
        parts.append(f"command={rule.command!r}")
    parts.append(f"decision={rule.decision!r}")
    return " ".join(parts)


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


def execution_mode_from_metadata(
    metadata: dict[str, object] | None,
) -> RuntimeExecutionMode:
    """Extract validated execution_mode from runtime request/session metadata.

    Falls back to DEFAULT_RUNTIME_EXECUTION_MODE when missing or invalid; the
    runtime contract layer is responsible for rejecting bad input upstream, so
    this helper is intentionally permissive at read time.
    """
    if metadata is None:
        return DEFAULT_RUNTIME_EXECUTION_MODE
    raw = metadata.get("execution_mode")
    if raw == "plan":
        return "plan"
    return DEFAULT_RUNTIME_EXECUTION_MODE
