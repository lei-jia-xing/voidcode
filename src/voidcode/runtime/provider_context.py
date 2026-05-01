from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import cast

from ..provider.model_catalog import ToolFeedbackMode
from ..tools.output import (
    redacted_argument_keys_for_tool,
    sanitize_tool_arguments,
    sanitize_tool_result_data,
    strip_redaction_sentinels,
)
from .context_window import RuntimeAssembledContext, RuntimeContextSegment
from .contracts import (
    RuntimeProviderContextDiagnostic,
    RuntimeProviderContextDiagnosticPolicyAction,
    RuntimeProviderContextDiagnosticPolicyMode,
    RuntimeProviderContextPolicyDecision,
    RuntimeProviderContextSegmentSnapshot,
    RuntimeProviderContextSnapshot,
    RuntimeProviderMessageSnapshot,
)

_MAX_DEBUG_CONTENT_CHARS = 2_000
_OVERSIZED_TOOL_FEEDBACK_CHARS = 8_000
_PROVIDER_CONTEXT_POLICY_BLOCKING_CODES = frozenset(
    {
        "missing_tool_result",
        "orphan_tool_result",
        "duplicate_tool_call_id",
        "oversized_tool_feedback",
        "provider_requires_tools_schema",
    }
)
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "password",
        "secret",
        "token",
    }
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(access[_-]?token\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(client[_-]?secret\s*[=:]\s*)[^\s,;]+"),
)


def inspect_provider_context(
    *,
    assembled_context: RuntimeAssembledContext,
    provider: str,
    model: str,
    execution_engine: str,
    available_tool_count: int,
    tool_feedback_mode: ToolFeedbackMode = "standard",
    oversized_tool_feedback_chars: int = _OVERSIZED_TOOL_FEEDBACK_CHARS,
    diagnostic_policy_mode: RuntimeProviderContextDiagnosticPolicyMode | None = None,
) -> RuntimeProviderContextSnapshot:
    segments = tuple(
        _segment_snapshot(index, segment)
        for index, segment in enumerate(assembled_context.segments)
    )
    provider_messages = tuple(
        _provider_message_snapshots(
            segments=assembled_context.segments,
            tool_feedback_mode=tool_feedback_mode,
        )
    )
    diagnostics = tuple(
        _diagnostics(
            segments=assembled_context.segments,
            context_metadata=assembled_context.metadata,
            tool_feedback_mode=tool_feedback_mode,
            available_tool_count=available_tool_count,
            oversized_tool_feedback_chars=oversized_tool_feedback_chars,
        )
    )
    policy_decision = (
        evaluate_provider_context_policy(diagnostics, mode=diagnostic_policy_mode)
        if diagnostic_policy_mode is not None
        else None
    )
    if policy_decision is not None:
        diagnostics = tuple(
            _diagnostic_with_policy_metadata(diagnostic, policy_decision)
            for diagnostic in diagnostics
        )
    return RuntimeProviderContextSnapshot(
        provider=provider,
        model=model,
        execution_engine=execution_engine,
        segment_count=len(segments),
        message_count=len(provider_messages),
        context_window=dict(assembled_context.metadata),
        segments=segments,
        provider_messages=provider_messages,
        diagnostics=diagnostics,
        policy_decision=policy_decision,
    )


def evaluate_provider_context_policy(
    diagnostics: tuple[RuntimeProviderContextDiagnostic, ...],
    *,
    mode: RuntimeProviderContextDiagnosticPolicyMode,
) -> RuntimeProviderContextPolicyDecision:
    actionable = tuple(
        diagnostic for diagnostic in diagnostics if diagnostic.severity in {"warning", "error"}
    )
    blocking = tuple(
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.severity == "error"
        or diagnostic.code in _PROVIDER_CONTEXT_POLICY_BLOCKING_CODES
    )
    diagnostic_codes = tuple(diagnostic.code for diagnostic in actionable)
    blocking_codes = tuple(diagnostic.code for diagnostic in blocking)
    if mode == "off":
        return RuntimeProviderContextPolicyDecision(
            mode=mode,
            action="ignored",
            blocked=False,
            diagnostic_count=len(actionable),
            diagnostic_codes=diagnostic_codes,
            blocking_diagnostic_codes=(),
            message="Provider-context diagnostics policy is off; diagnostics are debug-only.",
        )
    if mode == "block" and blocking:
        return RuntimeProviderContextPolicyDecision(
            mode=mode,
            action="block",
            blocked=True,
            diagnostic_count=len(actionable),
            diagnostic_codes=diagnostic_codes,
            blocking_diagnostic_codes=blocking_codes,
            message="Provider execution blocked by provider-context diagnostics policy.",
        )
    if actionable:
        return RuntimeProviderContextPolicyDecision(
            mode=mode,
            action="warn",
            blocked=False,
            diagnostic_count=len(actionable),
            diagnostic_codes=diagnostic_codes,
            blocking_diagnostic_codes=blocking_codes if mode == "block" else (),
            message="Provider-context diagnostics policy recorded warnings without blocking.",
        )
    return RuntimeProviderContextPolicyDecision(
        mode=mode,
        action="none",
        blocked=False,
        diagnostic_count=0,
        diagnostic_codes=(),
        blocking_diagnostic_codes=(),
    )


def _diagnostic_with_policy_metadata(
    diagnostic: RuntimeProviderContextDiagnostic,
    decision: RuntimeProviderContextPolicyDecision,
) -> RuntimeProviderContextDiagnostic:
    if decision.action in {"none", "ignored"}:
        action: RuntimeProviderContextDiagnosticPolicyAction = decision.action
        blocking = False
    elif decision.blocked and diagnostic.code in decision.blocking_diagnostic_codes:
        action = "block"
        blocking = True
    elif diagnostic.severity in {"warning", "error"}:
        action = "warn"
        blocking = False
    else:
        action = "none"
        blocking = False
    return RuntimeProviderContextDiagnostic(
        severity=diagnostic.severity,
        code=diagnostic.code,
        message=diagnostic.message,
        source=diagnostic.source,
        segment_indices=diagnostic.segment_indices,
        suggested_fix=diagnostic.suggested_fix,
        details={**diagnostic.details, "policy_mode": decision.mode},
        policy_action=action,
        policy_blocking=blocking,
    )


def _source_from_metadata(segment: RuntimeContextSegment) -> str:
    metadata = segment.metadata or {}
    source = metadata.get("source")
    return source if isinstance(source, str) and source else "assembled_context"


def _clip_content(content: str | None) -> tuple[str | None, bool]:
    if content is None:
        return None, False
    redacted = _redact_debug_text(content)
    if len(redacted) <= _MAX_DEBUG_CONTENT_CHARS:
        return redacted, False
    return f"{redacted[:_MAX_DEBUG_CONTENT_CHARS]}…", True


def _redact_debug_text(content: str) -> str:
    redacted = content
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(r"\1[redacted]", redacted)
    return redacted


def _normalize_tool_call_id(value: str | None, *, fallback: str) -> str:
    raw = value if value is not None and value.strip() else fallback
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())
    return normalized or fallback


def _safe_payload(value: object) -> object:
    if isinstance(value, str):
        clipped, truncated = _clip_content(value)
        return {"text": clipped, "truncated": True} if truncated else clipped
    if isinstance(value, dict):
        return {
            str(key): _safe_payload(item)
            for key, item in cast(dict[object, object], value).items()
            if str(key).lower() not in _SECRET_KEYS
        }
    if isinstance(value, list | tuple):
        return [_safe_payload(item) for item in cast(list[object] | tuple[object, ...], value)]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)


def _segment_snapshot(
    index: int,
    segment: RuntimeContextSegment,
) -> RuntimeProviderContextSegmentSnapshot:
    content, content_truncated = _clip_content(segment.content)
    metadata = segment.metadata or {}
    raw_data = metadata.get("data")
    safe_metadata = {key: value for key, value in metadata.items() if key not in {"data"}}
    if isinstance(raw_data, dict):
        safe_metadata["data"] = _sanitize_debug_data(cast(dict[str, object], raw_data))
    return RuntimeProviderContextSegmentSnapshot(
        index=index,
        role=segment.role,
        source=_source_from_metadata(segment),
        content=content,
        content_truncated=content_truncated,
        tool_call_id=segment.tool_call_id,
        tool_name=segment.tool_name,
        tool_arguments=_sanitize_debug_arguments(segment.tool_arguments or {}),
        metadata=cast(dict[str, object], _safe_payload(safe_metadata)),
    )


def _provider_message_snapshots(
    *,
    segments: tuple[RuntimeContextSegment, ...],
    tool_feedback_mode: ToolFeedbackMode,
) -> list[RuntimeProviderMessageSnapshot]:
    if tool_feedback_mode == "synthetic_user_message":
        return _synthetic_tool_feedback_message_snapshots(segments)
    return _standard_tool_message_snapshots(segments)


def _standard_tool_message_snapshots(
    segments: tuple[RuntimeContextSegment, ...],
) -> list[RuntimeProviderMessageSnapshot]:
    messages: list[RuntimeProviderMessageSnapshot] = []
    for segment in segments:
        if segment.role == "assistant" and segment.tool_name is not None:
            messages.append(
                RuntimeProviderMessageSnapshot(
                    index=len(messages),
                    role="assistant",
                    source="provider_native_tool_call",
                    tool_calls=(
                        {
                            "id": _tool_call_id(segment, fallback=segment.tool_name),
                            "type": "function",
                            "function": {
                                "name": segment.tool_name,
                                "arguments": json.dumps(
                                    _provider_visible_debug_arguments(
                                        segment.tool_name,
                                        segment.tool_arguments or {},
                                    ),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            },
                        },
                    ),
                )
            )
            continue
        if segment.role == "tool":
            content, content_truncated = _clip_content(_tool_payload_json(segment))
            messages.append(
                RuntimeProviderMessageSnapshot(
                    index=len(messages),
                    role="tool",
                    source="provider_native_tool_result",
                    content=content,
                    content_truncated=content_truncated,
                    tool_call_id=_tool_call_id(
                        segment,
                        fallback=segment.tool_name or "voidcode_tool",
                    ),
                )
            )
            continue
        content, content_truncated = _clip_content(segment.content)
        messages.append(
            RuntimeProviderMessageSnapshot(
                index=len(messages),
                role=segment.role,
                source=_source_from_metadata(segment),
                content=content,
                content_truncated=content_truncated,
            )
        )
    return messages


def _synthetic_tool_feedback_message_snapshots(
    segments: tuple[RuntimeContextSegment, ...],
) -> list[RuntimeProviderMessageSnapshot]:
    messages: list[RuntimeProviderMessageSnapshot] = []
    tool_feedback_lines: list[str] = []
    for segment in segments:
        if segment.role == "tool":
            tool_feedback_lines.append(_tool_payload_json(segment))
        elif segment.role != "assistant":
            content, content_truncated = _clip_content(segment.content)
            messages.append(
                RuntimeProviderMessageSnapshot(
                    index=len(messages),
                    role=segment.role,
                    source=_source_from_metadata(segment),
                    content=content,
                    content_truncated=content_truncated,
                )
            )
    if tool_feedback_lines:
        content, content_truncated = _clip_content(
            "\n".join(
                (
                    "Completed tool calls for current request:",
                    "Use these results as latest state. Do not repeat completed calls "
                    "unless retry is required.",
                    *tool_feedback_lines,
                )
            )
        )
        messages.append(
            RuntimeProviderMessageSnapshot(
                index=len(messages),
                role="user",
                source="provider_synthetic_tool_feedback",
                content=content,
                content_truncated=content_truncated,
            )
        )
    return messages


def _tool_payload_json(segment: RuntimeContextSegment) -> str:
    metadata = segment.metadata or {}
    raw_data = metadata.get("data")
    sanitized_data = (
        _sanitize_debug_data(cast(dict[str, object], raw_data))
        if isinstance(raw_data, dict)
        else {}
    )
    raw_arguments = sanitized_data.get("arguments")
    sanitized_arguments = (
        _provider_visible_debug_arguments(
            segment.tool_name,
            cast(dict[str, object], raw_arguments),
        )
        if isinstance(raw_arguments, dict)
        else {}
    )
    payload = {
        "tool_name": segment.tool_name,
        "arguments": sanitized_arguments,
        "status": metadata.get("status"),
        "content": _redact_debug_text(segment.content or ""),
        "error": _safe_payload(metadata.get("error")),
        "data": {
            key: value
            for key, value in sanitized_data.items()
            if key not in {"tool_call_id", "arguments"}
        },
        "truncated": metadata.get("truncated"),
        "partial": metadata.get("partial"),
        "reference": metadata.get("reference"),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _sanitize_debug_arguments(arguments: dict[str, object]) -> dict[str, object]:
    sanitized = sanitize_tool_arguments(arguments)
    return cast(dict[str, object], _safe_payload(sanitized))


def _provider_visible_debug_arguments(
    tool_name: str | None,
    arguments: dict[str, object],
) -> dict[str, object]:
    sanitized = sanitize_tool_arguments(arguments)
    stripped = strip_redaction_sentinels(
        sanitized,
        redacted_keys=redacted_argument_keys_for_tool(tool_name),
    )
    safe = _safe_payload(stripped)
    return cast(dict[str, object], safe) if isinstance(safe, dict) else {}


def _sanitize_debug_data(data: dict[str, object]) -> dict[str, object]:
    sanitized = sanitize_tool_result_data(data)
    return cast(dict[str, object], _safe_payload(sanitized))


def _tool_call_id(segment: RuntimeContextSegment, *, fallback: str) -> str:
    return _normalize_tool_call_id(segment.tool_call_id, fallback=fallback)


def _diagnostics(
    *,
    segments: tuple[RuntimeContextSegment, ...],
    context_metadata: dict[str, object],
    tool_feedback_mode: ToolFeedbackMode,
    available_tool_count: int,
    oversized_tool_feedback_chars: int,
) -> list[RuntimeProviderContextDiagnostic]:
    diagnostics: list[RuntimeProviderContextDiagnostic] = []
    diagnostics.extend(_duplicate_system_diagnostics(segments))
    diagnostics.extend(_compact_projection_role_diagnostics(segments))
    diagnostics.extend(
        _tool_pair_diagnostics(
            segments,
            available_tool_count=available_tool_count,
            oversized_tool_feedback_chars=oversized_tool_feedback_chars,
        )
    )
    diagnostics.extend(_context_window_diagnostics(context_metadata))
    diagnostics.extend(_todo_projection_diagnostics(segments))
    if tool_feedback_mode == "synthetic_user_message" and any(
        segment.role == "tool" for segment in segments
    ):
        diagnostics.append(
            RuntimeProviderContextDiagnostic(
                severity="info",
                code="provider_path_uses_synthetic_tool_feedback",
                message=(
                    "Provider path receives completed tool results as one synthetic user "
                    "feedback block instead of provider-native tool-role messages."
                ),
                source="provider_synthetic_tool_feedback",
                suggested_fix=(
                    "Inspect provider_messages to verify each tool result appears exactly once."
                ),
            )
        )
    return diagnostics


def _duplicate_system_diagnostics(
    segments: tuple[RuntimeContextSegment, ...],
) -> list[RuntimeProviderContextDiagnostic]:
    by_fingerprint: dict[str, list[int]] = defaultdict(list)
    for index, segment in enumerate(segments):
        if segment.role != "system" or not isinstance(segment.content, str):
            continue
        fingerprint = " ".join(segment.content.lower().split())
        by_fingerprint[fingerprint].append(index)
    return [
        RuntimeProviderContextDiagnostic(
            severity="warning",
            code="duplicate_system_segment",
            message="Provider context contains duplicate normalized system text.",
            source="assembled_context",
            segment_indices=tuple(indices),
            suggested_fix="Check agent, skill, preserved system, and continuity injection sources.",
        )
        for indices in by_fingerprint.values()
        if len(indices) > 1
    ]


def _compact_projection_role_diagnostics(
    segments: tuple[RuntimeContextSegment, ...],
) -> list[RuntimeProviderContextDiagnostic]:
    compact_sources = {
        "continuity_summary",
        "runtime_context_artifact_reference",
    }
    diagnostics: list[RuntimeProviderContextDiagnostic] = []
    for index, segment in enumerate(segments):
        source = _source_from_metadata(segment)
        if source not in compact_sources:
            continue
        if segment.role == "system" and segment.tool_call_id is None and segment.tool_name is None:
            continue
        diagnostics.append(
            RuntimeProviderContextDiagnostic(
                severity="error",
                code="compact_projection_wrong_role",
                message=(
                    "Runtime compact projection segments must be system context, not "
                    "provider tool-role messages or synthetic assistant tool calls."
                ),
                source=source,
                segment_indices=(index,),
                suggested_fix=(
                    "Insert continuity summaries and artifact references as runtime-owned "
                    "system segments without tool_call_id or tool_name."
                ),
                details={"role": segment.role},
            )
        )
    return diagnostics


def _tool_pair_diagnostics(
    segments: tuple[RuntimeContextSegment, ...],
    *,
    available_tool_count: int,
    oversized_tool_feedback_chars: int,
) -> list[RuntimeProviderContextDiagnostic]:
    diagnostics: list[RuntimeProviderContextDiagnostic] = []
    assistant_ids: dict[str, list[int]] = defaultdict(list)
    tool_ids: dict[str, list[int]] = defaultdict(list)
    for index, segment in enumerate(segments):
        if segment.role == "assistant" and segment.tool_name is not None:
            assistant_ids[segment.tool_call_id or ""].append(index)
        if segment.role == "tool":
            tool_ids[segment.tool_call_id or ""].append(index)
            if len(segment.content or "") > oversized_tool_feedback_chars:
                diagnostics.append(
                    RuntimeProviderContextDiagnostic(
                        severity="warning",
                        code="oversized_tool_feedback",
                        message=(
                            "A retained tool result is large enough to pressure provider context."
                        ),
                        source=_source_from_metadata(segment),
                        segment_indices=(index,),
                        suggested_fix=(
                            "Prefer runtime-owned summaries or artifacts for large tool outputs."
                        ),
                        details={
                            "content_chars": len(segment.content or ""),
                            "threshold_chars": oversized_tool_feedback_chars,
                        },
                    )
                )
    for tool_call_id, indices in assistant_ids.items():
        if tool_call_id not in tool_ids:
            diagnostics.append(
                RuntimeProviderContextDiagnostic(
                    severity="error",
                    code="missing_tool_result",
                    message="Assistant tool call has no matching tool result segment.",
                    source="assembled_context",
                    segment_indices=tuple(indices),
                    suggested_fix=(
                        "Ensure every assistant tool call is followed by exactly one tool result."
                    ),
                    details={"tool_call_id": tool_call_id},
                )
            )
    for tool_call_id, indices in tool_ids.items():
        if tool_call_id not in assistant_ids:
            diagnostics.append(
                RuntimeProviderContextDiagnostic(
                    severity="error",
                    code="orphan_tool_result",
                    message="Tool result segment has no matching assistant tool call segment.",
                    source="assembled_context",
                    segment_indices=tuple(indices),
                    suggested_fix=(
                        "Rebuild provider context from paired runtime.tool_completed records."
                    ),
                    details={"tool_call_id": tool_call_id},
                )
            )
    duplicate_ids = sorted(
        {
            tool_call_id
            for ids_by_role in (assistant_ids, tool_ids)
            for tool_call_id, indices in ids_by_role.items()
            if len(indices) > 1
        }
    )
    if duplicate_ids:
        diagnostics.append(
            RuntimeProviderContextDiagnostic(
                severity="warning",
                code="duplicate_tool_call_id",
                message="Provider context reuses a tool_call_id across assistant tool calls.",
                source="assembled_context",
                suggested_fix="Check runtime invocation id and provider tool_call_id propagation.",
                details={"tool_call_ids": duplicate_ids},
            )
        )
    if assistant_ids and available_tool_count == 0:
        diagnostics.append(
            RuntimeProviderContextDiagnostic(
                severity="warning",
                code="provider_requires_tools_schema",
                message=(
                    "Provider history contains tool calls but the current request has no "
                    "active tool schema."
                ),
                source="tool_registry",
                suggested_fix=(
                    "Keep a compatible tool schema available or inject a provider-specific "
                    "compatibility guard."
                ),
            )
        )
    return diagnostics


def _context_window_diagnostics(
    context_metadata: dict[str, object],
) -> list[RuntimeProviderContextDiagnostic]:
    diagnostics: list[RuntimeProviderContextDiagnostic] = []
    dropped = context_metadata.get("dropped_tool_result_count")
    if isinstance(dropped, int) and dropped > 0:
        diagnostics.append(
            RuntimeProviderContextDiagnostic(
                severity="info",
                code="tool_feedback_not_retained",
                message=(
                    "Some historical tool results were dropped before provider context assembly."
                ),
                source="context_window",
                suggested_fix=(
                    "Inspect continuity_state and retained tool segments for critical state "
                    "coverage."
                ),
                details={"dropped_tool_result_count": dropped},
            )
        )
    continuity = context_metadata.get("continuity_state")
    continuity_payload = (
        cast(dict[str, object], continuity) if isinstance(continuity, dict) else None
    )
    if continuity_payload is not None:
        source = continuity_payload.get("distillation_source")
        if isinstance(source, str) and source:
            diagnostics.append(
                RuntimeProviderContextDiagnostic(
                    severity="info",
                    code="continuity_distillation_source",
                    message=("Continuity summary source recorded for provider context debugging."),
                    source="context_window",
                    details={"distillation_source": source},
                )
            )
    if continuity_payload is not None and not continuity_payload.get("summary_text") and dropped:
        diagnostics.append(
            RuntimeProviderContextDiagnostic(
                severity="warning",
                code="compaction_boundary_missing_checkpoint",
                message="Tool results were dropped without a continuity summary checkpoint.",
                source="context_window",
                suggested_fix=(
                    "Enable continuity summarization before dropping older tool feedback."
                ),
            )
        )
    return diagnostics


def _todo_projection_diagnostics(
    segments: tuple[RuntimeContextSegment, ...],
) -> list[RuntimeProviderContextDiagnostic]:
    has_runtime_todos = any(
        segment.role == "system" and _source_from_metadata(segment) == "runtime_todo_state"
        for segment in segments
    )
    todo_tool_indices = [
        index
        for index, segment in enumerate(segments)
        if segment.role == "tool" and segment.tool_name == "todo_write"
    ]
    if todo_tool_indices and not has_runtime_todos:
        return [
            RuntimeProviderContextDiagnostic(
                severity="warning",
                code="todo_state_only_in_droppable_feedback",
                message=(
                    "TODO/progress state is visible only through retained todo_write tool feedback."
                ),
                source="runtime_todo_state",
                segment_indices=tuple(todo_tool_indices),
                suggested_fix=(
                    "Persist TODO/progress as runtime-owned state before context-window pruning."
                ),
            )
        ]
    return []
