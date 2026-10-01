from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ..tools.contracts import ToolCall, ToolDefinition, is_read_tier

type PermissionDecision = Literal["allow", "deny", "ask"]
type PermissionResolution = Literal["allow", "deny"]
type PathScope = Literal["workspace", "external"]
type OperationClass = Literal["read", "write", "execute"]
#: The approval-mode vocabulary: one auto-approve threshold, applied per tool
#: tier. ``ask`` auto-approves ``read`` only, ``write`` also approves
#: ``write``, ``yolo`` approves every tier.
type ApprovalMode = Literal["ask", "write", "yolo"]
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("ask", "write", "yolo")
DEFAULT_APPROVAL_MODE: ApprovalMode = "yolo"
PLAN_MODE_DENIAL_REASON = "read-only mode is active; mutating tools are denied"


def approval_decision(*, mode: ApprovalMode, operation_class: OperationClass | None) -> PermissionDecision:
    """The single place that turns ``(approval mode, tool tier)`` into a decision.

    Every other module threads the mode and the per-call tier here; no caller
    may re-derive the matrix. ``None`` is the unclassified tier and follows the
    same safe default as ``operation_class_for_tool`` (``execute``).
    """
    tier: OperationClass = "execute" if operation_class is None else operation_class
    if mode == "yolo":
        return "allow"
    if mode == "write":
        return "allow" if tier in ("read", "write") else "ask"
    return "allow" if tier == "read" else "ask"


@dataclass(frozen=True, slots=True)
class PermissionOutcome:
    decision: PermissionDecision
    pending_approval: PendingApproval | None = None

    def __post_init__(self) -> None:
        if self.decision == "ask" and self.pending_approval is None:
            raise ValueError("ask decisions require a pending approval")


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """The active approval policy: one auto-approve threshold for every tier.

    The per-call tier comes from :func:`resolve_permission`'s
    ``operation_class``; :func:`approval_decision` is the only place the two are
    combined.
    """

    mode: ApprovalMode = DEFAULT_APPROVAL_MODE


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
    write: ExternalDirectoryPolicy = field(default_factory=lambda: ExternalDirectoryPolicy(rules=(("*", "ask"),)))
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


def is_read_only_blocked(
    *,
    read_only: bool,
    tool: ToolDefinition,
    operation_class: OperationClass | None = None,
) -> bool:
    """Return True when the read-only runtime stance must deny a tool call.

    ``read_only`` is the single shared gate derived from ``resolve_mode``
    (plan mode implies it, as does explicit ``read_only`` metadata): every
    non-read-only tool is denied regardless of approval policy or path scope,
    and any explicit write/execute operation class is denied even if the tool
    itself is advertised as read-only (defense in depth).
    """
    if not read_only:
        return False
    if operation_class == "read":
        return False
    if not is_read_tier(tool.effects):
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
    read_only: bool = False,
) -> PermissionOutcome:
    """Resolve one tool call into a decision and its pending-approval payload.

    Precedence, highest first: read-only/plan denial, then an explicit
    ``rule_decision``, then the path-scope read shortcut, then an
    external-directory decision, and finally the active mode's tier matrix
    (:func:`approval_decision`).
    """
    decision: PermissionDecision
    effective_surface = policy_surface
    plan_blocked = is_read_only_blocked(read_only=read_only, tool=tool, operation_class=operation_class)
    if plan_blocked:
        decision = "deny"
        effective_surface = "mode.plan"
    elif rule_decision is not None:
        decision = rule_decision
    elif path_scope == "workspace" and (operation_class == "read" or (operation_class is None and is_read_tier(tool.effects))):
        return PermissionOutcome(decision="allow")
    elif path_scope == "external" and external_decision is not None:
        decision = external_decision
    else:
        decision = approval_decision(mode=policy.mode, operation_class=operation_class)

    pending_approval = build_pending_approval(
        tool_call,
        decision=decision,
        owner_session_id=owner_session_id,
        owner_parent_session_id=owner_parent_session_id,
        delegated_task_id=delegated_task_id,
        path_scope=path_scope,
        operation_class=operation_class,
        canonical_path=canonical_path,
        matched_rule=matched_rule,
        policy_surface=effective_surface,
        **({"reason": PLAN_MODE_DENIAL_REASON} if plan_blocked else {}),
    )
    return PermissionOutcome(decision=decision, pending_approval=pending_approval)


def build_pending_approval(
    tool_call: ToolCall,
    *,
    decision: PermissionDecision,
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
        policy_mode=decision,
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
        if rule.command is not None and not _command_matches_rule(command=command, pattern=rule.command):
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
    return any(_path_matches_rule(normalized_path=path, pattern=pattern) for path in path_candidates)


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
