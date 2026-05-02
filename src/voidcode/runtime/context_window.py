from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, NamedTuple, cast

from ..tools.contracts import ToolResult
from .continuity_distillation import (
    ContinuityDistillationRecord,
    build_distillation_input_envelope,
    distillation_record_from_payload,
)
from .todos import render_provider_todo_state


def _empty_tool_limits() -> dict[str, int]:
    return {}


@dataclass(frozen=True, slots=True)
class DroppedToolResultDiagnostic:
    tool_name: str
    status: str
    index: int
    tool_call_id: str | None = None
    artifact_id: str | None = None
    artifact_status: str | None = None
    artifact_byte_count: int | None = None
    artifact_line_count: int | None = None
    reference: str | None = None
    path: str | None = None
    command: str | None = None
    pattern: str | None = None
    error_kind: str | None = None
    estimated_tokens: int | None = None
    truncated: bool = False
    partial: bool = False

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "tool_name": self.tool_name,
            "status": self.status,
            "index": self.index,
        }
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.artifact_id is not None:
            payload["artifact_id"] = self.artifact_id
        if self.artifact_status is not None:
            payload["artifact_status"] = self.artifact_status
        if self.artifact_byte_count is not None:
            payload["artifact_byte_count"] = self.artifact_byte_count
        if self.artifact_line_count is not None:
            payload["artifact_line_count"] = self.artifact_line_count
        if self.reference is not None:
            payload["reference"] = self.reference
        if self.path is not None:
            payload["path"] = self.path
        if self.command is not None:
            payload["command"] = self.command
        if self.pattern is not None:
            payload["pattern"] = self.pattern
        if self.error_kind is not None:
            payload["error_kind"] = self.error_kind
        if self.estimated_tokens is not None:
            payload["estimated_tokens"] = self.estimated_tokens
        if self.truncated:
            payload["truncated"] = True
        if self.partial:
            payload["partial"] = True
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeContinuityState:
    summary_text: str | None = None
    objective: str | None = None
    current_goal: str | None = None
    verbatim_user_constraints: tuple[str, ...] = ()
    progress_completed: tuple[str, ...] = ()
    blockers_open_questions: tuple[str, ...] = ()
    key_decisions: tuple[str, ...] = ()
    relevant_files_commands_errors: tuple[str, ...] = ()
    verification_state: tuple[str, ...] = ()
    delegated_task_summaries: tuple[str, ...] = ()
    recent_tail: tuple[str, ...] = ()
    dropped_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    source: str = "tool_result_window"
    distillation_source: str = "deterministic"
    distillation_error: str | None = None
    fact_reference_count: int = 0
    source_references: tuple[str, ...] = ()
    original_tool_result_tokens: int | None = None
    retained_tool_result_tokens: int | None = None
    dropped_tool_result_tokens: int | None = None
    token_budget: int | None = None
    token_estimate_source: str | None = None
    dropped_tool_results: tuple[DroppedToolResultDiagnostic, ...] = ()
    # Lightweight versioning for continuity state to aid reinjection/refresh
    # semantics. This is incremented when the shape evolves and is included
    # in the serialized payload so consumers can decide how to handle newer
    # fields.
    version: int = 2

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "summary_text": self.summary_text,
            "objective": self.objective,
            "current_goal": self.current_goal,
            "verbatim_user_constraints": list(self.verbatim_user_constraints),
            "progress_completed": list(self.progress_completed),
            "blockers_open_questions": list(self.blockers_open_questions),
            "key_decisions": list(self.key_decisions),
            "relevant_files_commands_errors": list(self.relevant_files_commands_errors),
            "verification_state": list(self.verification_state),
            "delegated_task_summaries": list(self.delegated_task_summaries),
            "recent_tail": list(self.recent_tail),
            "dropped_tool_result_count": self.dropped_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
            "source": self.source,
            "distillation_source": self.distillation_source,
            "fact_reference_count": self.fact_reference_count,
            "source_references": list(self.source_references),
            "version": self.version,
        }
        if self.distillation_error is not None:
            payload["distillation_error"] = self.distillation_error
        if self.original_tool_result_tokens is not None:
            payload["original_tool_result_tokens"] = self.original_tool_result_tokens
        if self.retained_tool_result_tokens is not None:
            payload["retained_tool_result_tokens"] = self.retained_tool_result_tokens
        if self.dropped_tool_result_tokens is not None:
            payload["dropped_tool_result_tokens"] = self.dropped_tool_result_tokens
        if self.token_budget is not None:
            payload["token_budget"] = self.token_budget
        if self.token_estimate_source is not None:
            payload["token_estimate_source"] = self.token_estimate_source
        if self.dropped_tool_results:
            payload["dropped_tool_results"] = [
                item.metadata_payload() for item in self.dropped_tool_results
            ]
        return payload


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    auto_compaction: bool = True
    max_tool_results: int = 8
    max_tool_result_tokens: int | None = None
    max_context_ratio: float | None = None
    model_context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    minimum_retained_tool_results: int = 1
    recent_tool_result_count: int = 1
    recent_tool_result_tokens: int | None = 3_000
    default_tool_result_tokens: int | None = 1_500
    per_tool_result_tokens: Mapping[str, int] = field(default_factory=_empty_tool_limits)
    tokenizer_model: str | None = None
    continuity_preview_items: int = 3
    continuity_preview_chars: int = 80
    context_pressure_threshold: float = 0.7
    context_pressure_cooldown_steps: int = 3
    continuity_distillation_enabled: bool = False
    continuity_distillation_max_input_items: int = 12
    continuity_distillation_max_input_chars: int = 4000
    importance_retention: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_tool_result_tokens", dict(self.per_tool_result_tokens))
        if self.max_tool_results < 0:
            raise ValueError("max_tool_results must be >= 0")
        if self.max_tool_result_tokens is not None and self.max_tool_result_tokens < 1:
            raise ValueError("max_tool_result_tokens must be >= 1 when provided")
        if self.max_context_ratio is not None and not 0 < self.max_context_ratio <= 1:
            raise ValueError("max_context_ratio must be > 0 and <= 1 when provided")
        if self.model_context_window_tokens is not None and self.model_context_window_tokens < 1:
            raise ValueError("model_context_window_tokens must be >= 1 when provided")
        if self.reserved_output_tokens is not None and self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must be >= 0 when provided")
        if self.minimum_retained_tool_results < 0:
            raise ValueError("minimum_retained_tool_results must be >= 0")
        if self.recent_tool_result_count < 0:
            raise ValueError("recent_tool_result_count must be >= 0")
        if self.recent_tool_result_tokens is not None and self.recent_tool_result_tokens < 1:
            raise ValueError("recent_tool_result_tokens must be >= 1 when provided")
        if self.default_tool_result_tokens is not None and self.default_tool_result_tokens < 1:
            raise ValueError("default_tool_result_tokens must be >= 1 when provided")
        for tool_name, limit in self.per_tool_result_tokens.items():
            if not tool_name:
                raise ValueError("per_tool_result_tokens tool names must be non-empty")
            if limit < 1:
                raise ValueError("per_tool_result_tokens limits must be >= 1")
        if self.tokenizer_model is not None and not self.tokenizer_model:
            raise ValueError("tokenizer_model must be non-empty when provided")
        if self.continuity_preview_items < 1:
            raise ValueError("continuity_preview_items must be >= 1")
        if self.continuity_preview_chars < 1:
            raise ValueError("continuity_preview_chars must be >= 1")
        if not 0 < self.context_pressure_threshold <= 1:
            raise ValueError("context_pressure_threshold must be > 0 and <= 1")
        if self.context_pressure_cooldown_steps < 1:
            raise ValueError("context_pressure_cooldown_steps must be >= 1")
        if self.continuity_distillation_max_input_items < 1:
            raise ValueError("continuity_distillation_max_input_items must be >= 1")
        if self.continuity_distillation_max_input_chars < 64:
            raise ValueError("continuity_distillation_max_input_chars must be >= 64")

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": 1,
            "auto_compaction": self.auto_compaction,
            "max_tool_results": self.max_tool_results,
            "minimum_retained_tool_results": self.minimum_retained_tool_results,
            "recent_tool_result_count": self.recent_tool_result_count,
            "continuity_preview_items": self.continuity_preview_items,
            "continuity_preview_chars": self.continuity_preview_chars,
        }
        if self.max_tool_result_tokens is not None:
            payload["max_tool_result_tokens"] = self.max_tool_result_tokens
        if self.max_context_ratio is not None:
            payload["max_context_ratio"] = self.max_context_ratio
        if self.model_context_window_tokens is not None:
            payload["model_context_window_tokens"] = self.model_context_window_tokens
        if self.reserved_output_tokens is not None:
            payload["reserved_output_tokens"] = self.reserved_output_tokens
        if self.recent_tool_result_tokens is not None:
            payload["recent_tool_result_tokens"] = self.recent_tool_result_tokens
        if self.default_tool_result_tokens is not None:
            payload["default_tool_result_tokens"] = self.default_tool_result_tokens
        if self.per_tool_result_tokens:
            payload["per_tool_result_tokens"] = dict(self.per_tool_result_tokens)
        if self.tokenizer_model is not None:
            payload["tokenizer_model"] = self.tokenizer_model
        payload["context_pressure_threshold"] = self.context_pressure_threshold
        payload["context_pressure_cooldown_steps"] = self.context_pressure_cooldown_steps
        payload["continuity_distillation_enabled"] = self.continuity_distillation_enabled
        payload["continuity_distillation_max_input_items"] = (
            self.continuity_distillation_max_input_items
        )
        payload["continuity_distillation_max_input_chars"] = (
            self.continuity_distillation_max_input_chars
        )
        payload["importance_retention"] = self.importance_retention
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeContextWindow:
    prompt: str
    tool_results: tuple[ToolResult, ...] = ()
    compacted: bool = False
    compaction_reason: str | None = None
    original_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    max_tool_result_count: int = 0
    original_tool_result_tokens: int | None = None
    retained_tool_result_tokens: int | None = None
    dropped_tool_result_tokens: int | None = None
    token_budget: int | None = None
    token_estimate_source: str | None = None
    model_context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    truncated_tool_result_count: int = 0
    continuity_state: RuntimeContinuityState | None = None
    summary_anchor: str | None = None
    summary_source: dict[str, int] | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "compacted": self.compacted,
            "compaction_reason": self.compaction_reason,
            "original_tool_result_count": self.original_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
            "max_tool_result_count": self.max_tool_result_count,
        }
        if self.original_tool_result_tokens is not None:
            payload["original_tool_result_tokens"] = self.original_tool_result_tokens
        if self.retained_tool_result_tokens is not None:
            payload["retained_tool_result_tokens"] = self.retained_tool_result_tokens
        if self.dropped_tool_result_tokens is not None:
            payload["dropped_tool_result_tokens"] = self.dropped_tool_result_tokens
        if self.token_budget is not None:
            payload["token_budget"] = self.token_budget
        if self.token_estimate_source is not None:
            payload["token_estimate_source"] = self.token_estimate_source
        if self.reserved_output_tokens is not None:
            payload["reserved_output_tokens"] = self.reserved_output_tokens
        if self.truncated_tool_result_count:
            payload["truncated_tool_result_count"] = self.truncated_tool_result_count
        if self.continuity_state is not None:
            payload["continuity_state"] = self.continuity_state.metadata_payload()
        if self.summary_anchor is not None:
            payload["summary_anchor"] = self.summary_anchor
        if self.summary_source is not None:
            payload["summary_source"] = dict(self.summary_source)
        return payload


@dataclass(frozen=True, slots=True)
class ToolResultProjection:
    prepared_results: tuple[ToolResult, ...]
    retained_indexes: tuple[int, ...]
    dropped_indexes: tuple[int, ...]
    retained_results: tuple[ToolResult, ...]
    dropped_results: tuple[ToolResult, ...]
    truncated_count: int
    original_tokens: int | None = None
    retained_tokens: int | None = None
    dropped_tokens: int | None = None
    token_budget: int | None = None
    token_estimate_source: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAssembledContext:
    prompt: str
    tool_results: tuple[ToolResult, ...]
    continuity_state: RuntimeContinuityState | None
    segments: tuple[RuntimeContextSegment, ...]
    metadata: dict[str, object]
    loaded_skills: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeContextSegment:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, object] | None = None
    metadata: dict[str, object] | None = None


def _tool_result_preview(result: ToolResult, *, max_preview_chars: int) -> str:
    parts = [result.tool_name, result.status]
    artifact_id = _artifact_metadata_string(result, "artifact_id")
    if artifact_id is not None:
        parts.append(f"artifact_id={artifact_id}")
        tool_call_id = _optional_tool_string(result, "tool_call_id")
        if tool_call_id is not None:
            parts.append(f"tool_call_id={tool_call_id}")
        byte_count = _artifact_metadata_int(result, "byte_count")
        if byte_count is not None:
            parts.append(f"byte_count={byte_count}")
        line_count = _artifact_metadata_int(result, "line_count")
        if line_count is not None:
            parts.append(f"line_count={line_count}")
        return " ".join(parts)
    path = result.data.get("path")
    if isinstance(path, str) and path:
        parts.append(f"path={path}")
    pattern = result.data.get("pattern")
    if isinstance(pattern, str) and pattern:
        parts.append(f"pattern={pattern}")
    command = result.data.get("command")
    if isinstance(command, str) and command:
        parts.append(f"command={command}")

    content = normalize_read_file_output(result.content)
    error = result.error.strip() if result.error else ""
    preview_source = content or error
    if preview_source:
        clipped = preview_source[:max_preview_chars]
        if len(preview_source) > max_preview_chars:
            clipped = f"{clipped}..."
        preview_label = "content_preview" if content else "error_preview"
        parts.append(f'{preview_label}="{clipped}"')
    return " ".join(parts)


def _metadata_string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = payload.get(key)
    if not isinstance(raw, list | tuple):
        return ()
    raw_items = cast(list[object] | tuple[object, ...], raw)
    values: list[str] = []
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
    return tuple(values)


def _optional_entry_string(entry: Mapping[str, object], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) and value else None


def _optional_entry_int(entry: Mapping[str, object], key: str) -> int | None:
    value = entry.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dropped_tool_diagnostics_from_metadata_payload(
    payload: Mapping[str, object],
) -> tuple[DroppedToolResultDiagnostic, ...]:
    raw = payload.get("dropped_tool_results")
    if not isinstance(raw, list | tuple):
        return ()
    diagnostics: list[DroppedToolResultDiagnostic] = []
    for item in cast(list[object] | tuple[object, ...], raw):
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, object], item)
        tool_name = entry.get("tool_name")
        status = entry.get("status")
        index = entry.get("index")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        if not isinstance(status, str) or not status:
            continue
        if not isinstance(index, int) or isinstance(index, bool):
            continue

        estimated_tokens = entry.get("estimated_tokens")
        diagnostics.append(
            DroppedToolResultDiagnostic(
                tool_name=tool_name,
                status=status,
                index=index,
                tool_call_id=_optional_entry_string(entry, "tool_call_id"),
                artifact_id=_optional_entry_string(entry, "artifact_id"),
                artifact_status=_optional_entry_string(entry, "artifact_status"),
                artifact_byte_count=_optional_entry_int(entry, "artifact_byte_count"),
                artifact_line_count=_optional_entry_int(entry, "artifact_line_count"),
                reference=_optional_entry_string(entry, "reference"),
                path=_optional_entry_string(entry, "path"),
                command=_optional_entry_string(entry, "command"),
                pattern=_optional_entry_string(entry, "pattern"),
                error_kind=_optional_entry_string(entry, "error_kind"),
                estimated_tokens=(
                    estimated_tokens
                    if isinstance(estimated_tokens, int) and not isinstance(estimated_tokens, bool)
                    else None
                ),
                truncated=entry.get("truncated") is True,
                partial=entry.get("partial") is True,
            )
        )
    return tuple(diagnostics)


def continuity_state_from_metadata_payload(
    payload: Mapping[str, object],
) -> RuntimeContinuityState | None:
    version = payload.get("version")
    if version is None:
        resolved_version = 1
    elif isinstance(version, int) and not isinstance(version, bool):
        resolved_version = version
    else:
        return None
    if resolved_version not in {1, 2}:
        return None

    summary_text = payload.get("summary_text")
    if summary_text is not None and not isinstance(summary_text, str):
        return None
    objective = payload.get("objective")
    if objective is not None and not isinstance(objective, str):
        objective = None
    current_goal = payload.get("current_goal")
    if current_goal is not None and not isinstance(current_goal, str):
        current_goal = None
    dropped = payload.get("dropped_tool_result_count")
    retained = payload.get("retained_tool_result_count")
    source = payload.get("source")
    distillation_source = payload.get("distillation_source")
    distillation_error = payload.get("distillation_error")
    fact_reference_count = payload.get("fact_reference_count")
    source_references = _metadata_string_tuple(payload, "source_references")
    if not isinstance(dropped, int) or isinstance(dropped, bool):
        return None
    if not isinstance(retained, int) or isinstance(retained, bool):
        return None
    if not isinstance(source, str):
        return None
    if distillation_source is None:
        distillation_source = "deterministic"
    if not isinstance(distillation_source, str):
        return None
    if distillation_error is not None and not isinstance(distillation_error, str):
        return None
    if fact_reference_count is None:
        fact_reference_count = 0
    if not isinstance(fact_reference_count, int) or isinstance(fact_reference_count, bool):
        return None

    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ValueError

    try:
        original_token_count = _optional_int(payload.get("original_tool_result_tokens"))
        retained_token_count = _optional_int(payload.get("retained_tool_result_tokens"))
        dropped_token_count = _optional_int(payload.get("dropped_tool_result_tokens"))
        resolved_token_budget = _optional_int(payload.get("token_budget"))
    except ValueError:
        return None
    token_estimate_source = payload.get("token_estimate_source")
    if token_estimate_source is not None and not isinstance(token_estimate_source, str):
        return None
    return RuntimeContinuityState(
        summary_text=summary_text,
        objective=objective,
        current_goal=current_goal,
        verbatim_user_constraints=_metadata_string_tuple(payload, "verbatim_user_constraints"),
        progress_completed=_metadata_string_tuple(payload, "progress_completed"),
        blockers_open_questions=_metadata_string_tuple(payload, "blockers_open_questions"),
        key_decisions=_metadata_string_tuple(payload, "key_decisions"),
        relevant_files_commands_errors=_metadata_string_tuple(
            payload,
            "relevant_files_commands_errors",
        ),
        verification_state=_metadata_string_tuple(payload, "verification_state"),
        delegated_task_summaries=_metadata_string_tuple(payload, "delegated_task_summaries"),
        recent_tail=_metadata_string_tuple(payload, "recent_tail"),
        dropped_tool_result_count=dropped,
        retained_tool_result_count=retained,
        source=source,
        distillation_source=distillation_source,
        distillation_error=distillation_error,
        fact_reference_count=fact_reference_count,
        source_references=source_references,
        original_tool_result_tokens=original_token_count,
        retained_tool_result_tokens=retained_token_count,
        dropped_tool_result_tokens=dropped_token_count,
        token_budget=resolved_token_budget,
        token_estimate_source=token_estimate_source,
        dropped_tool_results=_dropped_tool_diagnostics_from_metadata_payload(payload),
        version=resolved_version,
    )


def _previous_continuity_state(
    session_metadata: Mapping[str, object],
) -> RuntimeContinuityState | None:
    runtime_state = session_metadata.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return None
    continuity = cast(dict[str, object], runtime_state).get("continuity")
    if not isinstance(continuity, dict):
        return None
    return continuity_state_from_metadata_payload(cast(dict[str, object], continuity))


def _merge_unique_strings(*groups: tuple[str, ...], limit: int = 12) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            stripped = value.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            merged.append(stripped)
            if len(merged) >= limit:
                return tuple(merged)
    return tuple(merged)


def _line_preview(value: str, *, limit: int) -> str:
    collapsed = " ".join(part.strip() for part in value.splitlines() if part.strip())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}..."


def _constraint_lines(prompt: str) -> tuple[str, ...]:
    constraints: list[str] = []
    markers = ("must", "must not", "never", "always", "do not", "don't", "forbidden")
    for raw_line in prompt.splitlines():
        line = raw_line.strip(" -\t")
        lowered = line.lower()
        if line and any(marker in lowered for marker in markers):
            constraints.append(line)
    return tuple(constraints[:8])


def _facts_from_tool_results(
    results: tuple[ToolResult, ...], *, preview_item_limit: int, preview_char_limit: int
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    progress: list[str] = []
    blockers: list[str] = []
    refs: list[str] = []
    delegated: list[str] = []
    for result in results[:preview_item_limit]:
        preview = _tool_result_preview(result, max_preview_chars=preview_char_limit)
        if result.status == "ok":
            progress.append(f"Tool result compacted: {preview}")
        else:
            blockers.append(f"Tool error compacted: {preview}")
        path = result.data.get("path")
        if isinstance(path, str) and path:
            refs.append(f"file:{path}")
        command = result.data.get("command")
        if isinstance(command, str) and command:
            refs.append(f"command:{command}")
        if result.tool_name in {"task", "background_output"}:
            task_id = result.data.get("task_id")
            child_session_id = result.data.get("child_session_id")
            summary_output = result.data.get("summary_output")
            parts = [f"tool={result.tool_name}"]
            if isinstance(task_id, str):
                parts.append(f"task_id={task_id}")
            if isinstance(child_session_id, str):
                parts.append(f"child_session_id={child_session_id}")
            if isinstance(summary_output, str) and summary_output:
                parts.append(f"summary={_line_preview(summary_output, limit=preview_char_limit)}")
            delegated.append(" ".join(parts))
    return tuple(progress), tuple(blockers), tuple(refs), tuple(delegated)


def _continuity_summary_text(state: RuntimeContinuityState) -> str:
    sections: list[str] = []

    def add_section(title: str, values: tuple[str, ...] | str | None) -> None:
        if isinstance(values, str):
            value = values.strip()
            if value:
                sections.append(f"## {title}\n{value}")
            return
        if not values:
            return
        lines = "\n".join(f"- {value}" for value in values if value.strip())
        if lines:
            sections.append(f"## {title}\n{lines}")

    add_section("Objective", state.objective)
    add_section("Current Goal", state.current_goal)
    add_section("Constraints", state.verbatim_user_constraints)
    add_section("Progress Completed", state.progress_completed)
    add_section("Blockers / Open Questions", state.blockers_open_questions)
    add_section("Key Decisions", state.key_decisions)
    add_section("Relevant Files / Commands / Errors", state.relevant_files_commands_errors)
    add_section("Verification State", state.verification_state)
    add_section("Delegated / Background Tasks", state.delegated_task_summaries)
    add_section("Recent Verbatim Tail", state.recent_tail)
    sections.append(
        "## Compaction Metadata\n"
        f"- Dropped tool results: {state.dropped_tool_result_count}\n"
        f"- Retained tool results: {state.retained_tool_result_count}\n"
        f"- Source: {state.source}"
    )
    return "\n\n".join(sections)


_UNICODE_TOKEN_ESTIMATE_SOURCE = "unicode_aware_chars"

type TokenCountMethod = Literal["tiktoken", "estimated"]


@dataclass(frozen=True, slots=True)
class TokenCount:
    tokens: int
    method: TokenCountMethod
    source: str
    exact: bool = False

    def metadata_payload(self) -> dict[str, object]:
        return {
            "tokens": self.tokens,
            "method": self.method,
            "source": self.source,
            "exact": self.exact,
        }


class _TokenEstimate(NamedTuple):
    tokens: int
    source: str


@lru_cache(maxsize=32)
def _tiktoken_encoding_for_model(tokenizer_model: str) -> Any:
    tiktoken = cast(Any, importlib.import_module("tiktoken"))
    try:
        return tiktoken.encoding_for_model(tokenizer_model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_text_tokens(value: str, *, tokenizer_model: str | None = None) -> TokenCount:
    if not value:
        return TokenCount(0, method="estimated", source=_UNICODE_TOKEN_ESTIMATE_SOURCE)
    if tokenizer_model is not None:
        try:
            encoding = _tiktoken_encoding_for_model(tokenizer_model)
            return TokenCount(
                len(encoding.encode(value, disallowed_special=())),
                method="tiktoken",
                source=f"tiktoken:{tokenizer_model}",
                exact=True,
            )
        except ImportError:
            pass
    ascii_chars = 0
    non_ascii_chars = 0
    for char in value:
        if ord(char) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return TokenCount(
        tokens=max(1, ((ascii_chars + 3) // 4) + non_ascii_chars),
        method="estimated",
        source=_UNICODE_TOKEN_ESTIMATE_SOURCE,
        exact=False,
    )


def _estimated_token_count(value: str, *, tokenizer_model: str | None = None) -> _TokenEstimate:
    counted = count_text_tokens(value, tokenizer_model=tokenizer_model)
    return _TokenEstimate(counted.tokens, counted.source)


def _tool_result_token_estimate(
    result: ToolResult, *, tokenizer_model: str | None = None
) -> _TokenEstimate:
    payload = {
        "tool_name": result.tool_name,
        "status": result.status,
        "content": result.content,
        "error": result.error,
        "data": result.data,
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return _estimated_token_count(serialized, tokenizer_model=tokenizer_model)


def _optional_tool_string(result: ToolResult, key: str) -> str | None:
    value = result.data.get(key)
    return value if isinstance(value, str) and value else None


def _optional_tool_int(result: ToolResult, key: str) -> int | None:
    value = result.data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _artifact_metadata_value(result: ToolResult, key: str) -> object:
    artifact = result.data.get("artifact")
    if isinstance(artifact, Mapping):
        value = cast(Mapping[str, object], artifact).get(key)
        if value is not None:
            return value
    return result.data.get(key)


def _artifact_metadata_string(result: ToolResult, key: str) -> str | None:
    value = _artifact_metadata_value(result, key)
    return value if isinstance(value, str) and value else None


def _artifact_metadata_int(result: ToolResult, key: str) -> int | None:
    value = _artifact_metadata_value(result, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dropped_tool_diagnostics(
    results: tuple[ToolResult, ...],
    *,
    original_indexes: tuple[int, ...] | None = None,
    tokenizer_model: str | None = None,
) -> tuple[DroppedToolResultDiagnostic, ...]:
    diagnostics: list[DroppedToolResultDiagnostic] = []
    for position, result in enumerate(results):
        index = original_indexes[position] + 1 if original_indexes is not None else position + 1
        diagnostics.append(
            DroppedToolResultDiagnostic(
                tool_name=result.tool_name,
                status=result.status,
                index=index,
                tool_call_id=_optional_tool_string(result, "tool_call_id"),
                artifact_id=_artifact_metadata_string(result, "artifact_id"),
                artifact_status=_artifact_metadata_string(result, "status")
                or _optional_tool_string(result, "artifact_status"),
                artifact_byte_count=_artifact_metadata_int(result, "byte_count")
                or _optional_tool_int(result, "original_byte_count")
                or _optional_tool_int(result, "original_error_byte_count"),
                artifact_line_count=_artifact_metadata_int(result, "line_count")
                or _optional_tool_int(result, "original_line_count")
                or _optional_tool_int(result, "original_error_line_count"),
                reference=result.reference,
                path=_optional_tool_string(result, "path"),
                command=_optional_tool_string(result, "command"),
                pattern=_optional_tool_string(result, "pattern"),
                error_kind=result.error_kind,
                estimated_tokens=_tool_result_token_estimate(
                    result,
                    tokenizer_model=tokenizer_model,
                ).tokens,
                truncated=result.truncated,
                partial=result.partial,
            )
        )
    return tuple(diagnostics)


def _select_recent_tool_result_indexes(
    results: tuple[ToolResult, ...],
    *,
    max_tool_results: int,
    protected_recent_count: int,
) -> tuple[int, ...]:
    if not results:
        return ()
    count_limit = max(max_tool_results, min(protected_recent_count, len(results)))
    if max_tool_results == 0:
        count_limit = min(protected_recent_count, len(results))
    start = max(0, len(results) - count_limit)
    return tuple(range(start, len(results)))


def _retain_indexes_within_token_budget(
    results: tuple[ToolResult, ...],
    candidate_indexes: tuple[int, ...],
    *,
    token_budget: int,
    protected_recent_count: int,
    tokenizer_model: str | None,
) -> tuple[int, ...]:
    if not candidate_indexes:
        return ()
    protected_recent_count = min(protected_recent_count, len(results))
    protected_start = len(results) - protected_recent_count
    protected_indexes = {index for index in candidate_indexes if index >= protected_start}
    retained = set(protected_indexes)
    retained_tokens = sum(
        _tool_result_token_estimate(results[index], tokenizer_model=tokenizer_model).tokens
        for index in protected_indexes
    )
    for index in sorted(
        (index for index in candidate_indexes if index not in protected_indexes),
        reverse=True,
    ):
        estimate = _tool_result_token_estimate(
            results[index], tokenizer_model=tokenizer_model
        ).tokens
        if retained_tokens + estimate > token_budget:
            continue
        retained.add(index)
        retained_tokens += estimate
    return tuple(sorted(retained))


def _policy_token_budget(policy: ContextWindowPolicy) -> int | None:
    if policy.max_tool_result_tokens is not None:
        return policy.max_tool_result_tokens
    if policy.model_context_window_tokens is None:
        return None
    available_context = policy.model_context_window_tokens
    if policy.reserved_output_tokens is not None:
        available_context = max(1, available_context - policy.reserved_output_tokens)
    if policy.max_context_ratio is None:
        return available_context if policy.reserved_output_tokens is not None else None
    return max(1, int(available_context * policy.max_context_ratio))


def _tool_limit_for_result(result: ToolResult, policy: ContextWindowPolicy) -> int | None:
    return policy.per_tool_result_tokens.get(result.tool_name, policy.default_tool_result_tokens)


def _clip_plain_text_to_token_limit(text: str, *, limit: int, tokenizer_model: str | None) -> str:
    clipped: list[str] = []
    used = 0
    for char in text:
        char_tokens = _estimated_token_count(char, tokenizer_model=tokenizer_model).tokens
        if used + char_tokens > limit:
            break
        clipped.append(char)
        used += char_tokens
    candidate = "".join(clipped)
    while (
        candidate
        and _estimated_token_count(candidate, tokenizer_model=tokenizer_model).tokens > limit
    ):
        candidate = candidate[:-1]
    return candidate


def _truncation_message(*, omitted_chars: int) -> str:
    return f"\n[Tool output truncated by context window policy; omitted {omitted_chars} chars]"


def _clip_text_to_token_limit(text: str, *, limit: int, tokenizer_model: str | None) -> str:
    if _estimated_token_count(text, tokenizer_model=tokenizer_model).tokens <= limit:
        return text
    clipped = _clip_plain_text_to_token_limit(
        text,
        limit=limit,
        tokenizer_model=tokenizer_model,
    )
    while True:
        omitted = len(text) - len(clipped)
        truncation_message = _truncation_message(omitted_chars=omitted)
        candidate = f"{clipped}{truncation_message}"
        if _estimated_token_count(candidate, tokenizer_model=tokenizer_model).tokens <= limit:
            return candidate
        if not clipped:
            return _clip_plain_text_to_token_limit(
                truncation_message,
                limit=limit,
                tokenizer_model=tokenizer_model,
            )
        clipped = clipped[:-1]


def _truncate_tool_result_content(
    result: ToolResult,
    *,
    limit: int | None,
    tokenizer_model: str | None,
) -> tuple[ToolResult, bool]:
    if limit is None or result.content is None:
        return result, False
    original_estimate = _estimated_token_count(
        result.content,
        tokenizer_model=tokenizer_model,
    )
    if original_estimate.tokens <= limit:
        return result, False
    clipped = _clip_text_to_token_limit(
        result.content,
        limit=limit,
        tokenizer_model=tokenizer_model,
    )
    data = {
        **result.data,
        "context_window_truncated": True,
        "context_window_original_content_tokens": original_estimate.tokens,
        "context_window_content_token_limit": limit,
    }
    return (
        ToolResult(
            tool_name=result.tool_name,
            status=result.status,
            content=clipped,
            data=data,
            error=result.error,
            truncated=True,
            partial=True,
            attachment=result.attachment,
            timeout_seconds=result.timeout_seconds,
            source=result.source,
            fallback_reason=result.fallback_reason,
            reference=result.reference,
            error_kind=result.error_kind,
        ),
        True,
    )


def _retain_results_within_token_budget(
    tool_results: tuple[ToolResult, ...],
    *,
    token_budget: int,
    minimum_retained_results: int,
    tokenizer_model: str | None,
) -> tuple[ToolResult, ...]:
    if not tool_results:
        return ()

    retained_reversed: list[ToolResult] = []
    retained_tokens = 0
    minimum_count = min(minimum_retained_results, len(tool_results))
    for result in reversed(tool_results):
        estimate = _tool_result_token_estimate(result, tokenizer_model=tokenizer_model).tokens
        must_retain = len(retained_reversed) < minimum_count
        if not must_retain and retained_tokens + estimate > token_budget:
            break
        retained_reversed.append(result)
        retained_tokens += estimate
    return tuple(reversed(retained_reversed))


def _token_estimate_source(policy: ContextWindowPolicy, sample: str = "sample") -> str:
    return _estimated_token_count(sample, tokenizer_model=policy.tokenizer_model).source


def _coerce_optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"context window policy field '{key}' must be an integer")


def _coerce_int(payload: Mapping[str, object], key: str, *, default: int) -> int:
    if key not in payload:
        return default
    value = _coerce_optional_int(payload, key)
    if value is None:
        raise ValueError(f"context window policy field '{key}' must be an integer")
    return value


def _coerce_float(payload: Mapping[str, object], key: str, *, default: float) -> float:
    raw = payload.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"context window policy field '{key}' must be a number")
    return float(raw)


def _coerce_bool(payload: Mapping[str, object], key: str, *, default: bool) -> bool:
    raw = payload.get(key)
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ValueError(f"context window policy field '{key}' must be a boolean")
    return raw


def context_window_policy_from_payload(raw_payload: object) -> ContextWindowPolicy:
    if not isinstance(raw_payload, dict):
        raise ValueError("context window policy payload must be an object")
    payload = cast(dict[str, object], raw_payload)
    per_tool_raw = payload.get("per_tool_result_tokens")
    per_tool: dict[str, int] = {}
    if per_tool_raw is not None:
        if not isinstance(per_tool_raw, dict):
            raise ValueError(
                "context window policy field 'per_tool_result_tokens' must be an object"
            )
        for key, value in cast(dict[object, object], per_tool_raw).items():
            if not isinstance(key, str) or not key:
                raise ValueError("context window policy per-tool keys must be non-empty strings")
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("context window policy per-tool limits must be positive integers")
            per_tool[key] = value
    auto_compaction = payload.get("auto_compaction", True)
    if not isinstance(auto_compaction, bool):
        raise ValueError("context window policy field 'auto_compaction' must be a boolean")
    max_context_ratio = payload.get("max_context_ratio")
    if max_context_ratio is not None and not isinstance(max_context_ratio, int | float):
        raise ValueError("context window policy field 'max_context_ratio' must be a number")
    tokenizer_model = payload.get("tokenizer_model")
    if tokenizer_model is not None and not isinstance(tokenizer_model, str):
        raise ValueError("context window policy field 'tokenizer_model' must be a string")
    continuity_distillation_enabled = payload.get(
        "continuity_distillation_enabled",
        ContextWindowPolicy().continuity_distillation_enabled,
    )
    if not isinstance(continuity_distillation_enabled, bool):
        raise ValueError(
            "context window policy field 'continuity_distillation_enabled' must be a boolean"
        )
    return ContextWindowPolicy(
        auto_compaction=auto_compaction,
        max_tool_results=_coerce_int(
            payload,
            "max_tool_results",
            default=ContextWindowPolicy().max_tool_results,
        ),
        max_tool_result_tokens=_coerce_optional_int(payload, "max_tool_result_tokens"),
        max_context_ratio=float(max_context_ratio) if max_context_ratio is not None else None,
        model_context_window_tokens=_coerce_optional_int(payload, "model_context_window_tokens"),
        reserved_output_tokens=_coerce_optional_int(payload, "reserved_output_tokens"),
        minimum_retained_tool_results=_coerce_int(
            payload,
            "minimum_retained_tool_results",
            default=ContextWindowPolicy().minimum_retained_tool_results,
        ),
        recent_tool_result_count=_coerce_int(
            payload,
            "recent_tool_result_count",
            default=ContextWindowPolicy().recent_tool_result_count,
        ),
        recent_tool_result_tokens=_coerce_optional_int(payload, "recent_tool_result_tokens"),
        default_tool_result_tokens=_coerce_optional_int(payload, "default_tool_result_tokens"),
        per_tool_result_tokens=per_tool,
        tokenizer_model=tokenizer_model,
        continuity_preview_items=_coerce_int(
            payload,
            "continuity_preview_items",
            default=ContextWindowPolicy().continuity_preview_items,
        ),
        continuity_preview_chars=_coerce_int(
            payload,
            "continuity_preview_chars",
            default=ContextWindowPolicy().continuity_preview_chars,
        ),
        context_pressure_threshold=_coerce_float(
            payload,
            "context_pressure_threshold",
            default=ContextWindowPolicy().context_pressure_threshold,
        ),
        context_pressure_cooldown_steps=_coerce_int(
            payload,
            "context_pressure_cooldown_steps",
            default=ContextWindowPolicy().context_pressure_cooldown_steps,
        ),
        continuity_distillation_enabled=continuity_distillation_enabled,
        continuity_distillation_max_input_items=_coerce_int(
            payload,
            "continuity_distillation_max_input_items",
            default=ContextWindowPolicy().continuity_distillation_max_input_items,
        ),
        continuity_distillation_max_input_chars=_coerce_int(
            payload,
            "continuity_distillation_max_input_chars",
            default=ContextWindowPolicy().continuity_distillation_max_input_chars,
        ),
        importance_retention=_coerce_bool(
            payload,
            "importance_retention",
            default=ContextWindowPolicy().importance_retention,
        ),
    )


def normalize_tool_result_content(content: str | None) -> str | None:
    if not content:
        return content

    return normalize_read_file_output(content)


def normalize_read_file_output(content: str | None) -> str | None:
    if not content:
        return content

    stripped = content.strip()
    if not (stripped.startswith("<path>") and "<content>" in stripped and "</content>" in stripped):
        return content

    body_start = stripped.find("<content>") + len("<content>")
    body_end = stripped.rfind("</content>")
    body = stripped[body_start:body_end].strip()
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("("):
            if line.startswith("(Showing lines ") or line.startswith("(Output capped at "):
                lines.append(line)
            continue
        if ": " in raw_line:
            _, text = raw_line.split(": ", 1)
            lines.append(text)
            continue
        lines.append(line)
    return "\n".join(lines)


def _build_continuity_state(
    *,
    prompt: str,
    session_metadata: Mapping[str, object],
    dropped_results: tuple[ToolResult, ...],
    dropped_result_indexes: tuple[int, ...],
    retained_results: tuple[ToolResult, ...],
    retained_count: int,
    preview_item_limit: int,
    preview_char_limit: int,
    original_tokens: int | None = None,
    retained_tokens: int | None = None,
    dropped_tokens: int | None = None,
    token_budget: int | None = None,
    token_estimate_source: str | None = None,
    tokenizer_model: str | None = None,
    continuity_distillation_enabled: bool = False,
    continuity_distillation_max_input_items: int = 12,
    continuity_distillation_max_input_chars: int = 4000,
    distillation_candidate: Mapping[str, object] | None = None,
) -> RuntimeContinuityState:
    dropped_count = len(dropped_results)
    previous = _previous_continuity_state(session_metadata)
    objective = previous.objective if previous is not None else None
    if objective is None:
        objective = _line_preview(prompt, limit=160) if prompt.strip() else None
    current_goal = _line_preview(prompt, limit=160) if prompt.strip() else objective
    progress, blockers, refs, delegated = _facts_from_tool_results(
        dropped_results,
        preview_item_limit=preview_item_limit,
        preview_char_limit=preview_char_limit,
    )
    retained_tail = tuple(
        _tool_result_preview(result, max_preview_chars=preview_char_limit)
        for result in retained_results[-preview_item_limit:]
    )
    previous_constraints = previous.verbatim_user_constraints if previous is not None else ()
    previous_progress = previous.progress_completed if previous is not None else ()
    previous_blockers = previous.blockers_open_questions if previous is not None else ()
    previous_decisions = previous.key_decisions if previous is not None else ()
    previous_refs = previous.relevant_files_commands_errors if previous is not None else ()
    previous_verification = previous.verification_state if previous is not None else ()
    previous_delegated = previous.delegated_task_summaries if previous is not None else ()
    previous_tail = previous.recent_tail if previous is not None else ()
    constraints = _merge_unique_strings(previous_constraints, _constraint_lines(prompt), limit=12)
    if dropped_count == 0:
        return RuntimeContinuityState(
            objective=objective,
            current_goal=current_goal,
            verbatim_user_constraints=constraints,
            progress_completed=previous_progress,
            blockers_open_questions=previous_blockers,
            key_decisions=previous_decisions,
            relevant_files_commands_errors=previous_refs,
            verification_state=previous_verification,
            delegated_task_summaries=previous_delegated,
            recent_tail=_merge_unique_strings(retained_tail, previous_tail, limit=8),
            retained_tool_result_count=retained_count,
            original_tool_result_tokens=original_tokens,
            retained_tool_result_tokens=retained_tokens,
            dropped_tool_result_tokens=dropped_tokens,
            token_budget=token_budget,
            token_estimate_source=token_estimate_source,
            dropped_tool_results=previous.dropped_tool_results if previous is not None else (),
            distillation_source="deterministic",
            source_references=previous.source_references if previous is not None else (),
        )

    preview_count = min(preview_item_limit, dropped_count)
    lines = [f"Compacted {dropped_count} earlier tool results:"]
    for index, result in enumerate(dropped_results[:preview_count], start=1):
        lines.append(
            f"{index}. {_tool_result_preview(result, max_preview_chars=preview_char_limit)}"
        )
    remaining = dropped_count - preview_count
    if remaining > 0:
        lines.append(f"... and {remaining} more")
    legacy_summary = "\n".join(lines)
    state_without_summary = RuntimeContinuityState(
        objective=objective,
        current_goal=current_goal,
        verbatim_user_constraints=constraints,
        progress_completed=_merge_unique_strings(previous_progress, progress, limit=16),
        blockers_open_questions=_merge_unique_strings(previous_blockers, blockers, limit=12),
        key_decisions=previous_decisions,
        relevant_files_commands_errors=_merge_unique_strings(previous_refs, refs, limit=16),
        verification_state=previous_verification,
        delegated_task_summaries=_merge_unique_strings(previous_delegated, delegated, limit=12),
        recent_tail=_merge_unique_strings(retained_tail, previous_tail, limit=8),
        dropped_tool_result_count=dropped_count,
        retained_tool_result_count=retained_count,
        source="tool_result_window",
        original_tool_result_tokens=original_tokens,
        retained_tool_result_tokens=retained_tokens,
        dropped_tool_result_tokens=dropped_tokens,
        token_budget=token_budget,
        token_estimate_source=token_estimate_source,
        dropped_tool_results=_dropped_tool_diagnostics(
            dropped_results,
            original_indexes=dropped_result_indexes,
            tokenizer_model=tokenizer_model,
        ),
        distillation_source="deterministic",
    )
    if previous is not None:
        previous_payload = previous.metadata_payload()
    else:
        previous_payload = None

    if continuity_distillation_enabled:
        effective_distillation, distillation_error = _try_model_assisted_distillation(
            prompt=prompt,
            dropped_results=dropped_results,
            retained_results=retained_results,
            previous_continuity=previous_payload,
            policy_items=continuity_distillation_max_input_items,
            policy_chars=continuity_distillation_max_input_chars,
            distillation_candidate=distillation_candidate,
        )
        if effective_distillation is not None:
            return _continuity_state_from_distillation(
                base_state=state_without_summary,
                distillation=effective_distillation,
            )
        if distillation_error is not None:
            fallback_summary = _continuity_summary_text(state_without_summary)
            fallback_text = (
                f"{fallback_summary}\n\n## Distillation Fallback\n- {distillation_error}"
            )
            return RuntimeContinuityState(
                summary_text=fallback_text,
                objective=state_without_summary.objective,
                current_goal=state_without_summary.current_goal,
                verbatim_user_constraints=state_without_summary.verbatim_user_constraints,
                progress_completed=state_without_summary.progress_completed,
                blockers_open_questions=state_without_summary.blockers_open_questions,
                key_decisions=state_without_summary.key_decisions,
                relevant_files_commands_errors=state_without_summary.relevant_files_commands_errors,
                verification_state=state_without_summary.verification_state,
                delegated_task_summaries=state_without_summary.delegated_task_summaries,
                recent_tail=state_without_summary.recent_tail,
                dropped_tool_result_count=state_without_summary.dropped_tool_result_count,
                retained_tool_result_count=state_without_summary.retained_tool_result_count,
                source=state_without_summary.source,
                distillation_source="fallback_after_model_error",
                distillation_error=distillation_error,
                original_tool_result_tokens=state_without_summary.original_tool_result_tokens,
                retained_tool_result_tokens=state_without_summary.retained_tool_result_tokens,
                dropped_tool_result_tokens=state_without_summary.dropped_tool_result_tokens,
                token_budget=state_without_summary.token_budget,
                token_estimate_source=state_without_summary.token_estimate_source,
                dropped_tool_results=state_without_summary.dropped_tool_results,
                fact_reference_count=0,
                source_references=(),
                version=2,
            )

    canonical_summary = _continuity_summary_text(state_without_summary)
    return RuntimeContinuityState(
        summary_text=f"{canonical_summary}\n\n## Dropped Tool Preview\n{legacy_summary}",
        objective=state_without_summary.objective,
        current_goal=state_without_summary.current_goal,
        verbatim_user_constraints=state_without_summary.verbatim_user_constraints,
        progress_completed=state_without_summary.progress_completed,
        blockers_open_questions=state_without_summary.blockers_open_questions,
        key_decisions=state_without_summary.key_decisions,
        relevant_files_commands_errors=state_without_summary.relevant_files_commands_errors,
        verification_state=state_without_summary.verification_state,
        delegated_task_summaries=state_without_summary.delegated_task_summaries,
        recent_tail=state_without_summary.recent_tail,
        dropped_tool_result_count=state_without_summary.dropped_tool_result_count,
        retained_tool_result_count=state_without_summary.retained_tool_result_count,
        source=state_without_summary.source,
        original_tool_result_tokens=state_without_summary.original_tool_result_tokens,
        retained_tool_result_tokens=state_without_summary.retained_tool_result_tokens,
        dropped_tool_result_tokens=state_without_summary.dropped_tool_result_tokens,
        token_budget=state_without_summary.token_budget,
        token_estimate_source=state_without_summary.token_estimate_source,
        dropped_tool_results=state_without_summary.dropped_tool_results,
        distillation_source="deterministic",
        source_references=state_without_summary.source_references,
        version=2,
    )


def _try_model_assisted_distillation(
    *,
    prompt: str,
    dropped_results: tuple[ToolResult, ...],
    retained_results: tuple[ToolResult, ...],
    previous_continuity: Mapping[str, object] | None,
    policy_items: int,
    policy_chars: int,
    distillation_candidate: Mapping[str, object] | None,
) -> tuple[ContinuityDistillationRecord | None, str | None]:
    envelope = build_distillation_input_envelope(
        prompt=prompt,
        dropped_results=dropped_results,
        retained_results=retained_results,
        previous_continuity=previous_continuity,
        max_items=max(1, policy_items),
        max_chars=max(64, policy_chars * 16),
    )
    candidate: Mapping[str, object] | None = distillation_candidate
    if candidate is None:
        embedded = envelope.get("distillation_candidate")
        if isinstance(embedded, dict):
            candidate = cast(Mapping[str, object], embedded)
    if candidate is None:
        return None, None
    parsed = distillation_record_from_payload(candidate)
    if parsed is None:
        return None, "model-assisted distillation output failed schema validation"
    return parsed, None


def _continuity_state_from_distillation(
    *,
    base_state: RuntimeContinuityState,
    distillation: ContinuityDistillationRecord,
) -> RuntimeContinuityState:
    key_decisions = tuple(
        f"{item.text} — {item.rationale}" for item in distillation.key_decisions_with_rationale
    )
    refs = tuple(item.text for item in distillation.relevant_files_commands_errors)
    source_references = _aggregate_distillation_source_references(distillation)
    verification = (
        distillation.verification_state.status,
        *distillation.verification_state.details,
    )
    summary = _continuity_summary_text(
        RuntimeContinuityState(
            summary_text=None,
            objective=base_state.objective,
            current_goal=distillation.objective_current_goal,
            verbatim_user_constraints=distillation.verbatim_user_constraints,
            progress_completed=distillation.completed_progress,
            blockers_open_questions=distillation.blockers_open_questions,
            key_decisions=key_decisions,
            relevant_files_commands_errors=refs,
            verification_state=verification,
            delegated_task_summaries=base_state.delegated_task_summaries,
            recent_tail=base_state.recent_tail,
            dropped_tool_result_count=base_state.dropped_tool_result_count,
            retained_tool_result_count=base_state.retained_tool_result_count,
            source=base_state.source,
            distillation_source="model_assisted",
            original_tool_result_tokens=base_state.original_tool_result_tokens,
            retained_tool_result_tokens=base_state.retained_tool_result_tokens,
            dropped_tool_result_tokens=base_state.dropped_tool_result_tokens,
            token_budget=base_state.token_budget,
            token_estimate_source=base_state.token_estimate_source,
            dropped_tool_results=base_state.dropped_tool_results,
            fact_reference_count=len(source_references),
            source_references=source_references,
        )
    )
    return RuntimeContinuityState(
        summary_text=summary,
        objective=base_state.objective,
        current_goal=distillation.objective_current_goal,
        verbatim_user_constraints=distillation.verbatim_user_constraints,
        progress_completed=distillation.completed_progress,
        blockers_open_questions=distillation.blockers_open_questions,
        key_decisions=key_decisions,
        relevant_files_commands_errors=refs,
        verification_state=verification,
        delegated_task_summaries=base_state.delegated_task_summaries,
        recent_tail=base_state.recent_tail,
        dropped_tool_result_count=base_state.dropped_tool_result_count,
        retained_tool_result_count=base_state.retained_tool_result_count,
        source=base_state.source,
        distillation_source="model_assisted",
        original_tool_result_tokens=base_state.original_tool_result_tokens,
        retained_tool_result_tokens=base_state.retained_tool_result_tokens,
        dropped_tool_result_tokens=base_state.dropped_tool_result_tokens,
        token_budget=base_state.token_budget,
        token_estimate_source=base_state.token_estimate_source,
        dropped_tool_results=base_state.dropped_tool_results,
        fact_reference_count=len(source_references),
        source_references=source_references,
        version=2,
    )


def _aggregate_distillation_source_references(
    distillation: ContinuityDistillationRecord,
) -> tuple[str, ...]:
    refs: list[str] = []

    def _append(kind: str, ref_id: str) -> None:
        value = f"{kind}:{ref_id}"
        if value not in refs:
            refs.append(value)

    for item in distillation.source_references:
        _append(item.kind, item.id)
    for item in distillation.key_decisions_with_rationale:
        for ref in item.refs:
            _append(ref.kind, ref.id)
    for item in distillation.relevant_files_commands_errors:
        for ref in item.refs:
            _append(ref.kind, ref.id)
    for ref in distillation.verification_state.refs:
        _append(ref.kind, ref.id)
    return tuple(refs)


def _summary_anchor(
    summary_text: str | None, *, dropped_count: int, retained_count: int
) -> str | None:
    if not summary_text:
        return None
    digest = hashlib.sha256(
        f"{dropped_count}:{retained_count}:{summary_text}".encode()
    ).hexdigest()[:16]
    return f"continuity:{digest}"


def continuity_summary_metadata(
    continuity_state: RuntimeContinuityState,
) -> tuple[str | None, dict[str, int] | None]:
    summary_anchor = _summary_anchor(
        continuity_state.summary_text,
        dropped_count=continuity_state.dropped_tool_result_count,
        retained_count=continuity_state.retained_tool_result_count,
    )
    summary_source = None
    if summary_anchor is not None and continuity_state.source == "tool_result_window":
        dropped_indexes = tuple(item.index for item in continuity_state.dropped_tool_results)
        if dropped_indexes == tuple(range(1, continuity_state.dropped_tool_result_count + 1)):
            summary_source = {
                "tool_result_start": 0,
                "tool_result_end": continuity_state.dropped_tool_result_count,
            }
    return summary_anchor, summary_source


def _artifact_reference_segments(
    continuity_state: RuntimeContinuityState | None,
) -> tuple[RuntimeContextSegment, ...]:
    if continuity_state is None:
        return ()
    segments: list[RuntimeContextSegment] = []
    for diagnostic in continuity_state.dropped_tool_results:
        if diagnostic.artifact_id is None:
            continue
        parts = [
            "Runtime artifact reference for omitted tool output:",
            f"artifact_id={diagnostic.artifact_id}",
            f"tool_call_id={diagnostic.tool_call_id}" if diagnostic.tool_call_id else None,
            f"tool_name={diagnostic.tool_name}",
            f"status={diagnostic.status}",
            (
                f"artifact_status={diagnostic.artifact_status}"
                if diagnostic.artifact_status
                else None
            ),
            (
                f"byte_count={diagnostic.artifact_byte_count}"
                if diagnostic.artifact_byte_count is not None
                else None
            ),
            (
                f"line_count={diagnostic.artifact_line_count}"
                if diagnostic.artifact_line_count is not None
                else None
            ),
            f"reference={diagnostic.reference}" if diagnostic.reference else None,
        ]
        content = "\n".join(part for part in parts if part is not None)
        metadata: dict[str, object] = {
            "source": "runtime_context_artifact_reference",
            "artifact_id": diagnostic.artifact_id,
            "tool_name": diagnostic.tool_name,
            "status": diagnostic.status,
            "dropped_tool_result_index": diagnostic.index,
        }
        if diagnostic.tool_call_id is not None:
            metadata["tool_call_id"] = diagnostic.tool_call_id
        if diagnostic.artifact_status is not None:
            metadata["artifact_status"] = diagnostic.artifact_status
        if diagnostic.artifact_byte_count is not None:
            metadata["byte_count"] = diagnostic.artifact_byte_count
        if diagnostic.artifact_line_count is not None:
            metadata["line_count"] = diagnostic.artifact_line_count
        if diagnostic.reference is not None:
            metadata["reference"] = diagnostic.reference
        segments.append(
            RuntimeContextSegment(
                role="system",
                content=content,
                metadata=metadata,
            )
        )
    return tuple(segments)


def project_tool_results_for_context_window(
    *,
    tool_results: tuple[ToolResult, ...],
    policy: ContextWindowPolicy,
) -> ToolResultProjection:
    token_budget = _policy_token_budget(policy)
    original_count = len(tool_results)
    protected_recent_count = max(
        policy.minimum_retained_tool_results,
        policy.recent_tool_result_count,
    )
    protected_recent_count = min(protected_recent_count, original_count)
    protected_start = original_count - protected_recent_count
    truncated_results: list[ToolResult] = []
    truncated_count = 0
    for index, result in enumerate(tool_results):
        if index >= protected_start:
            content_limit = policy.recent_tool_result_tokens
        else:
            content_limit = _tool_limit_for_result(result, policy)
        truncated_result, was_truncated = _truncate_tool_result_content(
            result,
            limit=content_limit,
            tokenizer_model=policy.tokenizer_model,
        )
        truncated_results.append(truncated_result)
        if was_truncated:
            truncated_count += 1
    prepared_results = tuple(truncated_results)

    count_limited_indexes = _select_recent_tool_result_indexes(
        prepared_results,
        max_tool_results=policy.max_tool_results,
        protected_recent_count=protected_recent_count,
    )
    retained_indexes = (
        _retain_indexes_within_token_budget(
            prepared_results,
            count_limited_indexes,
            token_budget=token_budget,
            protected_recent_count=protected_recent_count,
            tokenizer_model=policy.tokenizer_model,
        )
        if token_budget is not None
        else count_limited_indexes
    )
    retained_index_set = set(retained_indexes)
    dropped_indexes = tuple(
        index for index in range(len(prepared_results)) if index not in retained_index_set
    )
    retained_results = tuple(prepared_results[index] for index in retained_indexes)
    dropped_results = tuple(prepared_results[index] for index in dropped_indexes)

    original_tokens = None
    retained_tokens = None
    dropped_tokens = None
    token_estimate_source = None
    if token_budget is not None:
        original_tokens = sum(
            _tool_result_token_estimate(
                result,
                tokenizer_model=policy.tokenizer_model,
            ).tokens
            for result in prepared_results
        )
        retained_tokens = sum(
            _tool_result_token_estimate(
                result,
                tokenizer_model=policy.tokenizer_model,
            ).tokens
            for result in retained_results
        )
        dropped_tokens = original_tokens - retained_tokens
        token_estimate_source = _token_estimate_source(policy)

    return ToolResultProjection(
        prepared_results=prepared_results,
        retained_indexes=retained_indexes,
        dropped_indexes=dropped_indexes,
        retained_results=retained_results,
        dropped_results=dropped_results,
        truncated_count=truncated_count,
        original_tokens=original_tokens,
        retained_tokens=retained_tokens,
        dropped_tokens=dropped_tokens,
        token_budget=token_budget,
        token_estimate_source=token_estimate_source,
    )


def prepare_provider_context(
    *,
    prompt: str,
    tool_results: tuple[ToolResult, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
) -> RuntimeContextWindow:
    runtime_state = session_metadata.get("runtime_state")
    distillation_candidate: Mapping[str, object] | None = None
    if isinstance(runtime_state, dict):
        raw_candidate = cast(dict[str, object], runtime_state).get("distillation_candidate")
        if isinstance(raw_candidate, dict):
            distillation_candidate = cast(Mapping[str, object], raw_candidate)
    effective_policy = policy or ContextWindowPolicy()
    original_count = len(tool_results)
    token_budget = _policy_token_budget(effective_policy)

    if not effective_policy.auto_compaction:
        retained_results = tool_results
        retained_count = len(retained_results)
        original_tokens = None
        retained_tokens = None
        if token_budget is not None:
            original_tokens = sum(
                _tool_result_token_estimate(
                    result,
                    tokenizer_model=effective_policy.tokenizer_model,
                ).tokens
                for result in tool_results
            )
            retained_tokens = original_tokens
        return RuntimeContextWindow(
            prompt=prompt,
            tool_results=retained_results,
            compacted=False,
            compaction_reason=None,
            original_tool_result_count=original_count,
            retained_tool_result_count=retained_count,
            max_tool_result_count=effective_policy.max_tool_results,
            original_tool_result_tokens=original_tokens,
            retained_tool_result_tokens=retained_tokens,
            dropped_tool_result_tokens=0 if token_budget is not None else None,
            token_budget=token_budget,
            token_estimate_source=(
                _token_estimate_source(effective_policy) if token_budget is not None else None
            ),
            model_context_window_tokens=effective_policy.model_context_window_tokens,
            reserved_output_tokens=effective_policy.reserved_output_tokens,
        )

    projection = project_tool_results_for_context_window(
        tool_results=tool_results,
        policy=effective_policy,
    )
    retained_results = projection.retained_results
    retained_count = len(retained_results)
    compacted = retained_count < original_count
    dropped_indexes = projection.dropped_indexes
    dropped_results = projection.dropped_results
    original_tokens = projection.original_tokens
    retained_tokens = projection.retained_tokens
    dropped_tokens = projection.dropped_tokens
    token_estimate_source = projection.token_estimate_source

    continuity_state = (
        _build_continuity_state(
            prompt=prompt,
            session_metadata=session_metadata,
            dropped_results=dropped_results,
            dropped_result_indexes=dropped_indexes,
            retained_results=retained_results,
            retained_count=retained_count,
            preview_item_limit=effective_policy.continuity_preview_items,
            preview_char_limit=effective_policy.continuity_preview_chars,
            original_tokens=original_tokens,
            retained_tokens=retained_tokens,
            dropped_tokens=dropped_tokens,
            token_budget=token_budget,
            token_estimate_source=token_estimate_source,
            tokenizer_model=effective_policy.tokenizer_model,
            continuity_distillation_enabled=effective_policy.continuity_distillation_enabled,
            continuity_distillation_max_input_items=(
                effective_policy.continuity_distillation_max_input_items
            ),
            continuity_distillation_max_input_chars=(
                effective_policy.continuity_distillation_max_input_chars
            ),
            distillation_candidate=distillation_candidate,
        )
        if compacted
        else None
    )
    summary_anchor, summary_source = (
        continuity_summary_metadata(continuity_state)
        if continuity_state is not None
        else (None, None)
    )
    return RuntimeContextWindow(
        prompt=prompt,
        tool_results=retained_results,
        compacted=compacted,
        compaction_reason="tool_result_window" if compacted else None,
        original_tool_result_count=original_count,
        retained_tool_result_count=retained_count,
        max_tool_result_count=effective_policy.max_tool_results,
        original_tool_result_tokens=original_tokens,
        retained_tool_result_tokens=retained_tokens,
        dropped_tool_result_tokens=dropped_tokens,
        token_budget=token_budget,
        token_estimate_source=token_estimate_source,
        model_context_window_tokens=effective_policy.model_context_window_tokens,
        reserved_output_tokens=effective_policy.reserved_output_tokens,
        truncated_tool_result_count=projection.truncated_count,
        continuity_state=continuity_state,
        summary_anchor=summary_anchor,
        summary_source=summary_source,
    )


def assemble_provider_context(
    *,
    prompt: str,
    tool_results: tuple[ToolResult, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
    skill_prompt_context: str = "",
    agent_prompt_context: str = "",
    hook_preset_context: str = "",
    preserved_system_segments: tuple[str, ...] = (),
    loaded_skills: tuple[dict[str, object], ...] = (),
    preserved_continuity_state: RuntimeContinuityState | None = None,
) -> RuntimeAssembledContext:
    context_window = prepare_provider_context(
        prompt=prompt,
        tool_results=tool_results,
        session_metadata=session_metadata,
        policy=policy,
    )
    segments: list[RuntimeContextSegment] = []
    seen_system_contents: set[str] = set()

    def _append_system_segment(content: str, *, source: str) -> None:
        normalized = content.strip()
        if not normalized or normalized in seen_system_contents:
            return
        seen_system_contents.add(normalized)
        segments.append(
            RuntimeContextSegment(
                role="system",
                content=normalized,
                metadata={"source": source},
            )
        )

    _append_system_segment(agent_prompt_context, source="agent_prompt")
    _append_system_segment(hook_preset_context, source="hook_preset_guidance")
    for segment_content in preserved_system_segments:
        _append_system_segment(segment_content, source="preserved_system_segment")
    _append_system_segment(skill_prompt_context, source="skill_prompt")
    todo_prompt_context = render_provider_todo_state(session_metadata)
    if todo_prompt_context is not None:
        _append_system_segment(todo_prompt_context, source="runtime_todo_state")
    continuity_state = (
        preserved_continuity_state
        or context_window.continuity_state
        or _previous_continuity_state(session_metadata)
    )
    metadata_payload = context_window.metadata_payload()
    if continuity_state is not None and "continuity_state" not in metadata_payload:
        metadata_payload["continuity_state"] = continuity_state.metadata_payload()
    if continuity_state is not None and "summary_anchor" not in metadata_payload:
        summary_anchor, summary_source = continuity_summary_metadata(continuity_state)
        if summary_anchor is not None:
            metadata_payload["summary_anchor"] = summary_anchor
        if summary_source is not None:
            metadata_payload["summary_source"] = summary_source
    if continuity_state is not None:
        summary_text = continuity_state.summary_text
        if isinstance(summary_text, str) and summary_text.strip():
            _append_system_segment(
                f"Runtime continuity summary:\n{summary_text.strip()}",
                source="continuity_summary",
            )
        segments.extend(_artifact_reference_segments(continuity_state))
    segments.append(
        RuntimeContextSegment(
            role="user",
            content=prompt,
            metadata={"source": "current_user_prompt"},
        )
    )
    for index, result in enumerate(context_window.tool_results, start=1):
        raw_tool_call_id = result.data.get("tool_call_id")
        tool_call_id = (
            raw_tool_call_id
            if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip()
            else f"voidcode_tool_{index}"
        )
        raw_arguments = result.data.get("arguments")
        tool_arguments: dict[str, object]
        if isinstance(raw_arguments, dict):
            tool_arguments = dict(cast(dict[str, object], raw_arguments))
        else:
            tool_arguments = {}
        segments.append(
            RuntimeContextSegment(
                role="assistant",
                content=None,
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                tool_arguments=tool_arguments,
                metadata={"source": "retained_tool_result"},
            )
        )
        segments.append(
            RuntimeContextSegment(
                role="tool",
                content=result.content or "",
                tool_call_id=tool_call_id,
                tool_name=result.tool_name,
                metadata={
                    "source": "retained_tool_result",
                    "status": result.status,
                    "error": result.error,
                    "data": result.data,
                    "truncated": result.truncated,
                    "partial": result.partial,
                    "reference": result.reference,
                },
            )
        )
    return RuntimeAssembledContext(
        prompt=prompt,
        tool_results=context_window.tool_results,
        continuity_state=continuity_state,
        segments=tuple(segments),
        metadata=metadata_payload,
        loaded_skills=loaded_skills,
    )
