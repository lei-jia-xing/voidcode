from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..tools.contracts import Tool, ToolCall, ToolDefinition
from .permission import (
    ExternalDirectoryPermissionConfig,
    OperationClass,
    PathScope,
    PatternPermissionRule,
    PermissionDecision,
    evaluate_external_directory_policy,
    evaluate_pattern_permission_rules,
)
from .permission_context import RuntimePermissionContextResolver


@dataclass(frozen=True, slots=True)
class PermissionEvaluation:
    path_scope: PathScope
    operation_class: OperationClass
    canonical_path: str | None
    rule_decision: PermissionDecision | None = None
    matched_rule: str | None = None
    policy_surface: str | None = None
    external_decision: PermissionDecision | None = None


def shell_command_for_tool_call(tool_call: ToolCall) -> str | None:
    if tool_call.tool_name != "shell_exec":
        return None
    command = tool_call.arguments.get("command")
    if isinstance(command, str) and command.strip():
        return command
    return None


@dataclass(slots=True)
class PermissionEngine:
    _context_resolver: RuntimePermissionContextResolver
    _permission_config: ExternalDirectoryPermissionConfig
    _patch_path_extractor: Callable[[str], tuple[str, ...]] = field(default=lambda _: ())

    def evaluate(
        self,
        *,
        tool: ToolDefinition,
        tool_instance: Tool,
        tool_call: ToolCall,
        permission_rules: tuple[PatternPermissionRule, ...],
    ) -> PermissionEvaluation:
        path_scope, canonical_path, operation_class, external_paths = (
            self._context_resolver.permission_context_for_tool_call(
                tool=tool,
                tool_instance=tool_instance,
                tool_call=tool_call,
                patch_path_extractor=self._patch_path_extractor,
            )
        )

        normalized_paths = self._context_resolver.normalized_permission_path_candidates(
            tool_call,
            external_paths,
            patch_path_extractor=self._patch_path_extractor,
            tool=tool,
        )

        shell_command = shell_command_for_tool_call(tool_call)

        pattern_match = evaluate_pattern_permission_rules(
            rules=permission_rules,
            tool_name=tool_call.tool_name,
            path_candidates=normalized_paths,
            command=shell_command,
        )

        external_decision: PermissionDecision | None = None
        matched_rule: str | None = None
        policy_surface: str | None = None
        rule_decision: PermissionDecision | None = None

        if path_scope == "external" and canonical_path is not None:
            uses_write_policy = operation_class in ("write", "execute")
            policy_surface = (
                "external_directory_write" if uses_write_policy else "external_directory_read"
            )
            policy_config = self._permission_config
            rw = policy_config.write if uses_write_policy else policy_config.read
            decisions: list[tuple[PermissionDecision, str, str]] = []
            for external_path in external_paths:
                decision, rule = evaluate_external_directory_policy(
                    policy=rw,
                    canonical_path=Path(external_path),
                )
                decisions.append((decision, rule, external_path))

            deny_match = next((item for item in decisions if item[0] == "deny"), None)
            ask_match = next((item for item in decisions if item[0] == "ask"), None)
            allow_match = decisions[0] if decisions else None
            selected = deny_match or ask_match or allow_match
            if selected is not None:
                external_decision, matched_rule, canonical_path = selected
            if pattern_match is not None and external_decision is not None:
                candidate_decision, candidate_rule = pattern_match
                decision_rank: dict[PermissionDecision, int] = {"allow": 0, "ask": 1, "deny": 2}
                if decision_rank[candidate_decision] > decision_rank[external_decision]:
                    rule_decision = candidate_decision
                    matched_rule = candidate_rule
                    policy_surface = "permission.rules"
        else:
            if pattern_match is not None:
                rule_decision, matched_rule = pattern_match
                policy_surface = "permission.rules"
                canonical_path = normalized_paths[0] if normalized_paths else None

        return PermissionEvaluation(
            path_scope=path_scope,
            operation_class=operation_class,
            canonical_path=canonical_path,
            rule_decision=rule_decision,
            matched_rule=matched_rule,
            policy_surface=policy_surface,
            external_decision=external_decision,
        )
