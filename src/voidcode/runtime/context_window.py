from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, NamedTuple, cast

from ..agent.prompt_sections import dynamic_boundary_marker
from ..tools.contracts import ToolResult, ToolResultStatus
from .context_projection import project_summary
from .context_transforms import (
    RuntimeContextTransformResult,
    build_provider_context_transform_result,
)
from .prompt_assembly import (
    PromptAssemblyPlan,
    PromptAssemblySection,
    build_prompt_assembly_plan,
    prompt_activation_decision,
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
class ContextProjection:
    # Canonical context projection identity. The runtime treats this object as
    # the provider-facing projection produced by compaction.
    projection_id: str | None = None
    source_event_sequence: int | None = None
    source_checkpoint_id: str | None = None
    summary_text: str | None = None
    objective: str | None = None
    files_changed: tuple[str, ...] = ()
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
    version: int = 3

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "projection_id": self.projection_id,
            "source_event_sequence": self.source_event_sequence,
            "source_checkpoint_id": self.source_checkpoint_id,
            "summary_text": self.summary_text,
            "objective": self.objective,
            "files_changed": list(self.files_changed),
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
            "source_references": list(self.source_references),
            "version": self.version,
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
        if self.dropped_tool_results:
            payload["dropped_tool_results"] = [item.metadata_payload() for item in self.dropped_tool_results]
        return payload


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    # Behavior flip: whole-context budget trimming is opt-in only. The default
    # path retains every tool result (per-tool cap truncation is the only
    # automatic clipping) so an over-budget context surfaces as an explicit
    # provider overflow instead of silent data loss.
    auto_compaction: bool = False
    model_context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    default_tool_result_tokens: int | None = 1_500
    per_tool_result_tokens: Mapping[str, int] = field(default_factory=_empty_tool_limits)
    tokenizer_model: str | None = "cl100k_base"
    summary_strategy: Literal["deterministic", "model_assisted"] = "deterministic"

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_tool_result_tokens", dict(self.per_tool_result_tokens))
        if self.model_context_window_tokens is not None and self.model_context_window_tokens < 1:
            raise ValueError("model_context_window_tokens must be >= 1 when provided")
        if self.reserved_output_tokens is not None and self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must be >= 0 when provided")
        if self.default_tool_result_tokens is not None and self.default_tool_result_tokens < 1:
            raise ValueError("default_tool_result_tokens must be >= 1 when provided")
        for tool_name, limit in self.per_tool_result_tokens.items():
            if not tool_name:
                raise ValueError("per_tool_result_tokens tool names must be non-empty")
            if limit < 1:
                raise ValueError("per_tool_result_tokens limits must be >= 1")
        if self.tokenizer_model is not None and not self.tokenizer_model:
            raise ValueError("tokenizer_model must be non-empty when provided")

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": 1,
            "auto_compaction": self.auto_compaction,
            "summary_strategy": self.summary_strategy,
        }
        if self.model_context_window_tokens is not None:
            payload["model_context_window_tokens"] = self.model_context_window_tokens
        if self.reserved_output_tokens is not None:
            payload["reserved_output_tokens"] = self.reserved_output_tokens
        if self.default_tool_result_tokens is not None:
            payload["default_tool_result_tokens"] = self.default_tool_result_tokens
        if self.per_tool_result_tokens:
            payload["per_tool_result_tokens"] = dict(self.per_tool_result_tokens)
        if self.tokenizer_model is not None:
            payload["tokenizer_model"] = self.tokenizer_model
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeContextWindow:
    prompt: str
    tool_results: tuple[ToolResult | ToolResultView, ...] = ()
    compacted: bool = False
    compaction_reason: str | None = None
    original_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    original_tool_result_tokens: int | None = None
    retained_tool_result_tokens: int | None = None
    dropped_tool_result_tokens: int | None = None
    token_budget: int | None = None
    token_estimate_source: str | None = None
    model_context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    truncated_tool_result_count: int = 0
    continuity_state: ContextProjection | None = None
    summary_anchor: str | None = None
    summary_source: dict[str, int] | None = None
    summary_strategy: Literal["deterministic", "model_assisted", "fallback"] = "deterministic"
    summary_fallback_reason: str | None = None

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "compacted": self.compacted,
            "compaction_reason": self.compaction_reason,
            "original_tool_result_count": self.original_tool_result_count,
            "retained_tool_result_count": self.retained_tool_result_count,
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
        if self.model_context_window_tokens is not None:
            payload["model_context_window_tokens"] = self.model_context_window_tokens
        if self.reserved_output_tokens is not None:
            payload["reserved_output_tokens"] = self.reserved_output_tokens
        if self.truncated_tool_result_count:
            payload["truncated_tool_result_count"] = self.truncated_tool_result_count
        if self.continuity_state is not None:
            payload["projection"] = self.continuity_state.metadata_payload()
        if self.summary_anchor is not None:
            payload["summary_anchor"] = self.summary_anchor
        if self.summary_source is not None:
            payload["summary_source"] = dict(self.summary_source)
        payload["summary_strategy"] = self.summary_strategy
        if self.summary_fallback_reason is not None:
            payload["summary_fallback_reason"] = self.summary_fallback_reason
        return payload


@dataclass(frozen=True, slots=True)
class ToolResultView:
    """Provider-facing rendering view of a tool result.

    The persisted truth is the original ``ToolResult`` (event stream, session,
    checkpoints) and is never rebuilt or mutated by context-window trimming.
    Per-tool-cap truncation is a rendering-layer concern: this view carries the
    clipped content plus truncation statistics, and every other attribute is
    delegated to the source result. Views are transient provider-view artifacts
    and are never persisted.
    """

    result: ToolResult
    content: str | None
    # Whether the context window policy clipped ``content`` (per-tool cap).
    clipped: bool = False
    original_content_tokens: int | None = None
    content_token_limit: int | None = None

    @property
    def tool_name(self) -> str:
        return self.result.tool_name

    @property
    def status(self) -> ToolResultStatus:
        return self.result.status

    @property
    def data(self) -> dict[str, object]:
        return self.result.data

    @property
    def error(self) -> str | None:
        return self.result.error

    @property
    def truncated(self) -> bool:
        # Effective truncation: policy clipping or a source-truncated result.
        return self.clipped or self.result.truncated

    @property
    def partial(self) -> bool:
        # Policy clipping always renders a partial result; otherwise the
        # source result's own partial state is preserved untouched.
        return True if self.clipped else self.result.partial

    @property
    def reference(self) -> str | None:
        return self.result.reference

    @property
    def error_kind(self) -> str | None:
        return self.result.error_kind

    def __getattr__(self, name: str) -> object:
        return getattr(self.result, name)


@dataclass(frozen=True, slots=True)
class ToolResultProjection:
    prepared_results: tuple[ToolResultView, ...]
    retained_indexes: tuple[int, ...]
    dropped_indexes: tuple[int, ...]
    retained_results: tuple[ToolResultView, ...]
    dropped_results: tuple[ToolResultView, ...]
    truncated_count: int
    original_tokens: int | None = None
    retained_tokens: int | None = None
    dropped_tokens: int | None = None
    token_budget: int | None = None
    token_estimate_source: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAssembledContext:
    prompt: str
    tool_results: tuple[ToolResult | ToolResultView, ...]
    continuity_state: ContextProjection | None
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


def _context_tier_metadata(
    segments: list[RuntimeContextSegment],
) -> dict[str, object]:
    order: list[str] = []
    counts: dict[str, int] = {"instruction": 0, "workspace": 0, "task": 0, "recent": 0}
    for segment in segments:
        metadata = segment.metadata or {}
        if metadata.get("source") == "runtime_dynamic_boundary":
            continue
        raw_tier = metadata.get("tier")
        if raw_tier not in counts:
            continue
        tier = cast(Literal["instruction", "workspace", "task", "recent"], raw_tier)
        counts[tier] += 1
        if tier not in order:
            order.append(tier)
    return {
        "version": 1,
        "order": order,
        "counts": counts,
    }


def estimate_provider_context_tokens(segments: tuple[RuntimeContextSegment, ...], *, tokenizer_model: str | None = None) -> TokenCount:
    payload: list[dict[str, object]] = []
    for segment in segments:
        entry: dict[str, object] = {"role": segment.role}
        if segment.content is not None:
            entry["content"] = segment.content
        if segment.tool_call_id is not None:
            entry["tool_call_id"] = segment.tool_call_id
        if segment.tool_name is not None:
            entry["tool_name"] = segment.tool_name
        if segment.tool_arguments is not None:
            entry["tool_arguments"] = segment.tool_arguments
        payload.append(entry)
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return count_text_tokens(serialized, tokenizer_model=tokenizer_model)


def _tool_result_preview(result: ToolResult | ToolResultView, *, max_preview_chars: int) -> str:
    parts = [result.tool_name, result.status]
    artifact_id = _artifact_metadata_string(result, "artifact_id")
    if artifact_id is not None:
        parts.append(f"artifact_id={artifact_id}")
        parts.append(f"uri=voidcode://artifact/{artifact_id}")
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

    content = normalize_read_output(result.content)
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
                estimated_tokens=(estimated_tokens if isinstance(estimated_tokens, int) and not isinstance(estimated_tokens, bool) else None),
                truncated=entry.get("truncated") is True,
                partial=entry.get("partial") is True,
            )
        )
    return tuple(diagnostics)


def continuity_state_from_metadata_payload(
    payload: Mapping[str, object],
) -> ContextProjection | None:
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if version != 3:
        return None

    summary_text = payload.get("summary_text")
    if summary_text is not None and not isinstance(summary_text, str):
        return None
    objective = payload.get("objective")
    if objective is not None and not isinstance(objective, str):
        objective = None
    dropped = payload.get("dropped_tool_result_count")
    retained = payload.get("retained_tool_result_count")
    source = payload.get("source")
    source_references = _metadata_string_tuple(payload, "source_references")
    if not isinstance(dropped, int) or isinstance(dropped, bool):
        return None
    if not isinstance(retained, int) or isinstance(retained, bool):
        return None
    if not isinstance(source, str):
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
    return ContextProjection(
        summary_text=summary_text,
        objective=objective,
        files_changed=_metadata_string_tuple(payload, "files_changed"),
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
        source_references=source_references,
        original_tool_result_tokens=original_token_count,
        retained_tool_result_tokens=retained_token_count,
        dropped_tool_result_tokens=dropped_token_count,
        token_budget=resolved_token_budget,
        token_estimate_source=token_estimate_source,
        dropped_tool_results=_dropped_tool_diagnostics_from_metadata_payload(payload),
        version=version,
    )


def _previous_continuity_state(
    session_metadata: Mapping[str, object],
) -> ContextProjection | None:
    # 延迟导入：session_metadata_helpers 在模块级导入本模块（context_window），
    # 模块级反向导入会构成环。解析统一走 helpers parse/accessor（唯一入口）。
    from .session_metadata_helpers import (
        parse_runtime_state_metadata,
        runtime_state_context_projection,
        runtime_state_context_projection_summary,
    )

    runtime_state = parse_runtime_state_metadata(session_metadata.get("runtime_state"))
    if "continuity" in runtime_state or "continuity_summary" in runtime_state:
        raise ValueError("legacy runtime continuity metadata is no longer supported; start a new session")
    continuity = runtime_state_context_projection(session_metadata)
    if continuity is None:
        return None
    state = continuity_state_from_metadata_payload(continuity)
    if state is None or state.projection_id is not None:
        return state
    summary = runtime_state_context_projection_summary(session_metadata)
    if summary is not None and isinstance(summary.get("anchor"), str):
        return replace(state, projection_id=summary["anchor"])
    return state


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
    results: tuple[ToolResult | ToolResultView, ...], *, preview_item_limit: int, preview_char_limit: int
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    progress: list[str] = []
    blockers: list[str] = []
    refs: list[str] = []
    delegated: list[str] = []
    for result in results[:preview_item_limit]:
        if result.tool_name == "todo_write":
            continue
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


def _continuity_summary_text(state: ContextProjection) -> str:
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
    add_section("Constraints", state.verbatim_user_constraints)
    add_section("Progress Completed", state.progress_completed)
    add_section("Blockers / Open Questions", state.blockers_open_questions)
    add_section("Key Decisions", state.key_decisions)
    add_section("Relevant Files / Commands / Errors", state.relevant_files_commands_errors)
    add_section("Verification State", state.verification_state)
    add_section("Delegated / Background Tasks", state.delegated_task_summaries)
    add_section("Recent Verbatim Tail", state.recent_tail)
    return "\n\n".join(sections)


_CHARS_PER_TOKEN = 4
_APPROX_CHARS_PER_4_SOURCE = "approx_chars_per_4"

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


def count_text_tokens(value: str, *, tokenizer_model: str | None = None) -> TokenCount:
    _ = tokenizer_model
    if not value:
        return TokenCount(0, method="estimated", source=_APPROX_CHARS_PER_4_SOURCE)
    return TokenCount(
        tokens=max(1, (len(value) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN),
        method="estimated",
        source=_APPROX_CHARS_PER_4_SOURCE,
        exact=False,
    )


def _estimated_token_count(value: str, *, tokenizer_model: str | None = None) -> _TokenEstimate:
    counted = count_text_tokens(value, tokenizer_model=tokenizer_model)
    return _TokenEstimate(counted.tokens, counted.source)


def _tool_result_token_estimate(result: ToolResult | ToolResultView, *, tokenizer_model: str | None = None) -> _TokenEstimate:
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
    token_count = max(1, (len(serialized) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)
    return _TokenEstimate(token_count, _APPROX_CHARS_PER_4_SOURCE)


def _optional_tool_string(result: ToolResult | ToolResultView, key: str) -> str | None:
    value = result.data.get(key)
    return value if isinstance(value, str) and value else None


def _optional_tool_int(result: ToolResult | ToolResultView, key: str) -> int | None:
    value = result.data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _artifact_metadata_value(result: ToolResult | ToolResultView, key: str) -> object:
    artifact = result.data.get("artifact")
    if isinstance(artifact, Mapping):
        value = cast(Mapping[str, object], artifact).get(key)
        if value is not None:
            return value
    return result.data.get(key)


def _artifact_metadata_string(result: ToolResult | ToolResultView, key: str) -> str | None:
    value = _artifact_metadata_value(result, key)
    return value if isinstance(value, str) and value else None


def _artifact_metadata_int(result: ToolResult | ToolResultView, key: str) -> int | None:
    value = _artifact_metadata_value(result, key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dropped_tool_diagnostics(
    results: tuple[ToolResult | ToolResultView, ...],
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
                artifact_status=_artifact_metadata_string(result, "status") or _optional_tool_string(result, "artifact_status"),
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
    results: Sequence[ToolResult | ToolResultView],
) -> tuple[int, ...]:
    if not results:
        return ()
    return tuple(range(len(results)))


def _retain_indexes_within_token_budget(
    results: Sequence[ToolResult | ToolResultView],
    candidate_indexes: tuple[int, ...],
    *,
    token_budget: int,
    tokenizer_model: str | None,
) -> tuple[int, ...]:
    if not candidate_indexes:
        return ()
    retained: set[int] = set()
    retained_tokens = 0
    ordered_indexes = tuple(sorted(candidate_indexes, reverse=True))
    newest_index = ordered_indexes[0]
    newest_retained = False
    for index in ordered_indexes:
        estimate = _tool_result_token_estimate(results[index], tokenizer_model=tokenizer_model).tokens
        if index == newest_index:
            retained.add(index)
            retained_tokens = estimate
            newest_retained = True
            continue
        if retained_tokens + estimate > token_budget:
            continue
        retained.add(index)
        retained_tokens += estimate
    if retained and newest_retained:
        return tuple(sorted(retained))
    return (newest_index,)


def _policy_token_budget(policy: ContextWindowPolicy) -> int | None:
    if policy.model_context_window_tokens is None:
        return None
    if policy.reserved_output_tokens is not None:
        return max(1, policy.model_context_window_tokens - policy.reserved_output_tokens)
    return policy.model_context_window_tokens


def _tool_limit_for_result(result: ToolResult | ToolResultView, policy: ContextWindowPolicy) -> int | None:
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
    while candidate and _estimated_token_count(candidate, tokenizer_model=tokenizer_model).tokens > limit:
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


def _truncated_view_for_result(
    result: ToolResult | ToolResultView,
    *,
    limit: int | None,
    tokenizer_model: str | None,
) -> tuple[ToolResultView, bool]:
    """Return a provider rendering view for ``result``, clipping ``content``
    to the per-tool token cap when needed.

    The original ``ToolResult`` is never rebuilt or mutated; the returned view
    is the single object used both for token-budget accounting and for final
    provider rendering, so the budget decision always matches what the model
    sees. Already-prepared views pass through unchanged (no re-clipping).
    """
    if isinstance(result, ToolResultView):
        return result, False
    if limit is None or result.content is None:
        return ToolResultView(result=result, content=result.content), False
    original_estimate = _estimated_token_count(
        result.content,
        tokenizer_model=tokenizer_model,
    )
    if original_estimate.tokens <= limit:
        return ToolResultView(result=result, content=result.content), False
    clipped = _clip_text_to_token_limit(
        result.content,
        limit=limit,
        tokenizer_model=tokenizer_model,
    )
    return (
        ToolResultView(
            result=result,
            content=clipped,
            clipped=True,
            original_content_tokens=original_estimate.tokens,
            content_token_limit=limit,
        ),
        True,
    )


def _token_estimate_source(policy: ContextWindowPolicy, sample: str = "sample") -> str:
    return _APPROX_CHARS_PER_4_SOURCE


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


def normalize_read_output(content: str | None) -> str | None:
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
    dropped_results: tuple[ToolResult | ToolResultView, ...],
    dropped_result_indexes: tuple[int, ...],
    retained_results: tuple[ToolResult | ToolResultView, ...],
    retained_count: int,
    preview_item_limit: int,
    preview_char_limit: int,
    original_tokens: int | None = None,
    retained_tokens: int | None = None,
    dropped_tokens: int | None = None,
    token_budget: int | None = None,
    token_estimate_source: str | None = None,
    tokenizer_model: str | None = None,
) -> ContextProjection:
    dropped_count = len(dropped_results)
    previewable_dropped_results = tuple(result for result in dropped_results if result.tool_name != "todo_write")
    previous = _previous_continuity_state(session_metadata)
    objective = previous.objective if previous is not None else None
    if objective is None:
        objective = _line_preview(prompt, limit=160) if prompt.strip() else None
    progress, blockers, refs, delegated = _facts_from_tool_results(
        previewable_dropped_results,
        preview_item_limit=preview_item_limit,
        preview_char_limit=preview_char_limit,
    )
    retained_tail = tuple(_tool_result_preview(result, max_preview_chars=preview_char_limit) for result in retained_results[-preview_item_limit:])
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
        return ContextProjection(
            objective=objective,
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
            source_references=previous.source_references if previous is not None else (),
        )

    dropped_preview_summary = None
    if previewable_dropped_results:
        preview_count = min(preview_item_limit, len(previewable_dropped_results))
        lines = [f"Compacted {dropped_count} earlier tool results:"]
        for index, result in enumerate(previewable_dropped_results[:preview_count], start=1):
            lines.append(f"{index}. {_tool_result_preview(result, max_preview_chars=preview_char_limit)}")
        remaining = len(previewable_dropped_results) - preview_count
        if remaining > 0:
            lines.append(f"... and {remaining} more")
        dropped_preview_summary = "\n".join(lines)
    state_without_summary = ContextProjection(
        objective=objective,
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
    )

    canonical_summary = _continuity_summary_text(state_without_summary)
    summary_text = canonical_summary
    if dropped_preview_summary is not None:
        summary_text = f"{canonical_summary}\n\n## Dropped Tool Preview\n{dropped_preview_summary}"
    return ContextProjection(
        summary_text=summary_text,
        objective=state_without_summary.objective,
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
    )


def _summary_anchor(summary_text: str | None, *, dropped_count: int, retained_count: int) -> str | None:
    if not summary_text:
        return None
    digest = hashlib.sha256(f"{dropped_count}:{retained_count}:{summary_text}".encode()).hexdigest()[:16]
    return f"continuity:{digest}"


def continuity_summary_metadata(
    continuity_state: ContextProjection,
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
    continuity_state: ContextProjection | None,
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
            f"uri=voidcode://artifact/{diagnostic.artifact_id}",
            f"tool_call_id={diagnostic.tool_call_id}" if diagnostic.tool_call_id else None,
            f"tool_name={diagnostic.tool_name}",
            f"status={diagnostic.status}",
            (f"artifact_status={diagnostic.artifact_status}" if diagnostic.artifact_status else None),
            (f"byte_count={diagnostic.artifact_byte_count}" if diagnostic.artifact_byte_count is not None else None),
            (f"line_count={diagnostic.artifact_line_count}" if diagnostic.artifact_line_count is not None else None),
            f"reference={diagnostic.reference}" if diagnostic.reference else None,
            f'Read the omitted output with read(path="voidcode://artifact/{diagnostic.artifact_id}").',
        ]
        content = "\n".join(part for part in parts if part is not None)
        metadata: dict[str, object] = {
            "source": "runtime_context_artifact_reference",
            "artifact_id": diagnostic.artifact_id,
            "uri": f"voidcode://artifact/{diagnostic.artifact_id}",
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


def _pending_state_segment(session_metadata: Mapping[str, object]) -> RuntimeContextSegment | None:
    # 延迟导入：session_metadata_helpers 在模块级导入本模块（context_window），
    # 模块级反向导入会构成环。解析统一走 helpers parse（唯一入口）。
    from .session_metadata_helpers import parse_plan_state_metadata

    plan_state = parse_plan_state_metadata(session_metadata.get("plan_state"))
    status = plan_state.get("status")
    if status not in {"waiting_approval", "waiting_question", "waiting"}:
        return None
    blocked_tool = plan_state.get("blocked_tool")
    approval_request_id = plan_state.get("approval_request_id")
    parts = [f"Runtime pending state: {status}."]
    if isinstance(blocked_tool, str) and blocked_tool:
        parts.append(f"Blocked tool: {blocked_tool}.")
    if isinstance(approval_request_id, str) and approval_request_id:
        parts.append(f"Approval request id: {approval_request_id}.")
    if status == "waiting_approval":
        parts.append("Do not continue autonomous work until the approval is resolved through runtime resume.")
    elif status == "waiting_question":
        parts.append("Do not continue autonomous work until the user answers the pending question.")
    else:
        parts.append("Do not continue autonomous work until the pending runtime wait is resolved.")
    metadata: dict[str, object] = {"source": "runtime_pending_state", "status": status}
    if isinstance(blocked_tool, str) and blocked_tool:
        metadata["blocked_tool"] = blocked_tool
    if isinstance(approval_request_id, str) and approval_request_id:
        metadata["approval_request_id"] = approval_request_id
    return RuntimeContextSegment(
        role="system",
        content=" ".join(parts),
        metadata=metadata,
    )


def project_tool_results_for_context_window(
    *,
    tool_results: tuple[ToolResult | ToolResultView, ...],
    policy: ContextWindowPolicy,
) -> ToolResultProjection:
    token_budget = _policy_token_budget(policy)
    prepared_results: list[ToolResultView] = []
    truncated_count = 0
    for result in tool_results:
        content_limit = _tool_limit_for_result(result, policy)
        prepared_result, was_truncated = _truncated_view_for_result(
            result,
            limit=content_limit,
            tokenizer_model=policy.tokenizer_model,
        )
        prepared_results.append(prepared_result)
        if was_truncated:
            truncated_count += 1

    count_limited_indexes = _select_recent_tool_result_indexes(prepared_results)
    retained_indexes = (
        _retain_indexes_within_token_budget(
            prepared_results,
            count_limited_indexes,
            token_budget=token_budget,
            tokenizer_model=policy.tokenizer_model,
        )
        if token_budget is not None
        else _select_recent_tool_result_indexes(prepared_results)
    )
    retained_index_set = set(retained_indexes)
    dropped_indexes = tuple(index for index in range(len(prepared_results)) if index not in retained_index_set)
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
        prepared_results=tuple(prepared_results),
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
    tool_results: tuple[ToolResult | ToolResultView, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
    summary_projector: Callable[[Mapping[str, object]], str] | None = None,
) -> RuntimeContextWindow:
    effective_policy = policy or ContextWindowPolicy()
    original_count = len(tool_results)
    token_budget = _policy_token_budget(effective_policy)

    if not effective_policy.auto_compaction:
        # Default path: per-tool cap truncation is the only automatic clipping
        # (anti-explosion baseline). Every result is retained in full; an
        # over-budget context surfaces as an explicit provider overflow rather
        # than silent trimming. Views render the same clipped content the token
        # statistics below are computed from.
        prepared_views: list[ToolResultView] = []
        truncated_count = 0
        for result in tool_results:
            content_limit = _tool_limit_for_result(result, effective_policy)
            prepared_view, was_truncated = _truncated_view_for_result(
                result,
                limit=content_limit,
                tokenizer_model=effective_policy.tokenizer_model,
            )
            prepared_views.append(prepared_view)
            if was_truncated:
                truncated_count += 1
        retained_results = tuple(prepared_views)
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
            retained_tokens = sum(
                _tool_result_token_estimate(
                    result,
                    tokenizer_model=effective_policy.tokenizer_model,
                ).tokens
                for result in retained_results
            )
        return RuntimeContextWindow(
            prompt=prompt,
            tool_results=retained_results,
            compacted=False,
            compaction_reason=None,
            original_tool_result_count=original_count,
            retained_tool_result_count=retained_count,
            original_tool_result_tokens=original_tokens,
            retained_tool_result_tokens=retained_tokens,
            dropped_tool_result_tokens=0 if token_budget is not None else None,
            token_budget=token_budget,
            token_estimate_source=(_token_estimate_source(effective_policy) if token_budget is not None else None),
            model_context_window_tokens=effective_policy.model_context_window_tokens,
            reserved_output_tokens=effective_policy.reserved_output_tokens,
            truncated_tool_result_count=truncated_count,
            summary_strategy=("fallback" if effective_policy.summary_strategy == "model_assisted" else "deterministic"),
        )

    # Legacy explicit opt-in: whole-context budget trimming with continuity
    # projection. Retained for users who explicitly request aggressive
    # trimming; the default flow above never drops results.
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
            preview_item_limit=3,
            preview_char_limit=80,
            original_tokens=original_tokens,
            retained_tokens=retained_tokens,
            dropped_tokens=dropped_tokens,
            token_budget=token_budget,
            token_estimate_source=token_estimate_source,
            tokenizer_model=effective_policy.tokenizer_model,
        )
        if compacted
        else None
    )
    summary_strategy: Literal["deterministic", "model_assisted", "fallback"] = "deterministic"
    summary_fallback_reason: str | None = None
    if continuity_state is not None:
        projected_summary, summary_strategy, summary_fallback_reason = project_summary(
            strategy=effective_policy.summary_strategy,
            facts=continuity_state.metadata_payload(),
            deterministic_summary=continuity_state.summary_text or "",
            projector=summary_projector,
        )
        continuity_state = replace(continuity_state, summary_text=projected_summary)
    summary_anchor, summary_source = continuity_summary_metadata(continuity_state) if continuity_state is not None else (None, None)
    if continuity_state is not None and continuity_state.projection_id is None:
        continuity_state = replace(
            continuity_state,
            projection_id=summary_anchor,
        )
    return RuntimeContextWindow(
        prompt=prompt,
        tool_results=retained_results,
        compacted=compacted,
        compaction_reason="tool_result_window" if compacted else None,
        original_tool_result_count=original_count,
        retained_tool_result_count=retained_count,
        original_tool_result_tokens=original_tokens,
        retained_tool_result_tokens=retained_tokens,
        dropped_tool_result_tokens=dropped_tokens,
        token_budget=token_budget,
        token_estimate_source=token_estimate_source,
        model_context_window_tokens=effective_policy.model_context_window_tokens,
        reserved_output_tokens=effective_policy.reserved_output_tokens,
        summary_strategy=summary_strategy,
        summary_fallback_reason=summary_fallback_reason,
        truncated_tool_result_count=projection.truncated_count,
        continuity_state=continuity_state,
        summary_anchor=summary_anchor,
        summary_source=summary_source,
    )


def assemble_provider_context(
    *,
    prompt: str,
    tool_results: tuple[ToolResult | ToolResultView, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
    skill_prompt_context: str = "",
    agent_prompt_context: str = "",
    prompt_profile_name: str | None = None,
    hook_preset_context: str = "",
    context_transform_result: RuntimeContextTransformResult | None = None,
    loaded_skills: tuple[dict[str, object], ...] = (),
    preserved_continuity_state: ContextProjection | None = None,
    workspace_memory_context: str = "",
    workspace: Path | None = None,
    replay_retained_tool_messages: bool = True,
    replayed_conversation_segments: tuple[RuntimeContextSegment, ...] = (),
    summary_projector: Callable[[Mapping[str, object]], str] | None = None,
) -> RuntimeAssembledContext:
    context_window = prepare_provider_context(
        prompt=prompt,
        tool_results=tool_results,
        session_metadata=session_metadata,
        policy=policy,
        summary_projector=summary_projector,
    )
    transform_result = context_transform_result or build_provider_context_transform_result(
        workspace=workspace,
        tool_results=tool_results,
        hook_preset_context=hook_preset_context,
    )
    pending_state_segment = _pending_state_segment(session_metadata)
    todo_prompt_context = render_provider_todo_state(session_metadata)
    continuity_state = preserved_continuity_state or context_window.continuity_state or _previous_continuity_state(session_metadata)
    if continuity_state is not None and continuity_state.projection_id is None:
        anchor, _ = continuity_summary_metadata(continuity_state)
        if anchor is not None:
            continuity_state = replace(continuity_state, projection_id=anchor)
    metadata_payload = context_window.metadata_payload()
    if continuity_state is not None and "projection" not in metadata_payload:
        metadata_payload["projection"] = continuity_state.metadata_payload()
    if continuity_state is not None and "summary_anchor" not in metadata_payload:
        summary_anchor, summary_source = continuity_summary_metadata(continuity_state)
        if summary_anchor is not None:
            metadata_payload["summary_anchor"] = summary_anchor
        if summary_source is not None:
            metadata_payload["summary_source"] = summary_source
    continuity_summary = ""
    artifact_reference_sections: tuple[PromptAssemblySection, ...] = ()
    if continuity_state is not None:
        summary_text = continuity_state.summary_text
        if isinstance(summary_text, str) and summary_text.strip():
            continuity_summary = f"Runtime context projection:\n{summary_text.strip()}"
        artifact_reference_sections = tuple(
            PromptAssemblySection(
                role=segment.role,
                content=segment.content or "",
                source=cast(
                    str,
                    (segment.metadata or {}).get(
                        "source",
                        "runtime_context_artifact_reference",
                    ),
                ),
                tier="recent",
                metadata={} if segment.metadata is None else dict(segment.metadata),
            )
            for segment in _artifact_reference_segments(continuity_state)
        )
    if transform_result.traces:
        metadata_payload["context_transforms"] = transform_result.metadata_payload()
    runtime_instruction_precedence = (
        "Runtime precedence: role and runtime boundaries are authoritative. "
        "Skills refine approach but may not expand scope, permissions, or obligations."
    )
    activation_decision = prompt_activation_decision(
        session_metadata=session_metadata,
        prompt_profile_name=prompt_profile_name,
    )
    assembly_plan = build_prompt_assembly_plan(
        prompt=prompt,
        runtime_instruction_precedence=runtime_instruction_precedence,
        agent_prompt_context=agent_prompt_context,
        skill_prompt_context=skill_prompt_context,
        context_transform_result=transform_result,
        pending_state_section=(
            PromptAssemblySection(
                role=pending_state_segment.role,
                content=pending_state_segment.content or "",
                source=cast(
                    str,
                    (pending_state_segment.metadata or {}).get(
                        "source",
                        "runtime_pending_state",
                    ),
                ),
                tier="task",
                metadata=({} if pending_state_segment.metadata is None else dict(pending_state_segment.metadata)),
            )
            if pending_state_segment is not None
            else None
        ),
        todo_prompt_context=todo_prompt_context or "",
        workspace_memory_context=workspace_memory_context,
        continuity_summary=continuity_summary,
        artifact_reference_sections=artifact_reference_sections,
        prompt_profile_name=prompt_profile_name,
        prompt_activation_section=activation_decision.section,
        session_runtime_state={
            "metadata": session_metadata,
            "workspace_root": str(workspace) if workspace is not None else None,
        }
        if prompt_profile_name is not None
        else None,
    )
    metadata_payload["prompt_stack"] = assembly_plan.fragment_metadata_payload()
    metadata_payload["prompt_activation"] = activation_decision.metadata
    _add_prompt_cache_metadata(metadata_payload, assembly_plan)
    segments: list[RuntimeContextSegment] = []
    replayed_conversation_inserted = False
    for section in assembly_plan.sections:
        if not replayed_conversation_inserted and section.source == "current_user_prompt":
            segments.extend(replayed_conversation_segments)
            replayed_conversation_inserted = True
        segments.append(
            RuntimeContextSegment(
                role=section.role,
                content=section.content,
                metadata={
                    "source": section.source,
                    "tier": section.tier,
                    **dict(section.metadata),
                },
            )
        )
    if not replayed_conversation_inserted:
        raise RuntimeError("prompt assembly plan missing current_user_prompt section")
    if replay_retained_tool_messages:
        for index, result in enumerate(context_window.tool_results, start=1):
            if todo_prompt_context is not None and result.tool_name == "todo_write":
                continue
            # Prior-run results are already rendered inside the replayed
            # conversation history (before the current user prompt). Appending
            # them here would place previous-run tool messages after the new
            # prompt, making the model believe it is mid-turn and continue the
            # previous task instead of answering the new request.
            if getattr(result, "source", None) == "replayed_conversation":
                continue
            raw_tool_call_id = result.data.get("tool_call_id")
            tool_call_id = raw_tool_call_id if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip() else f"voidcode_tool_{index}"
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
                    metadata={"source": "retained_tool_result", "tier": "recent"},
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
                        "tier": "recent",
                        "status": result.status,
                        "error": result.error,
                        "data": result.data,
                        "truncated": result.truncated,
                        "partial": result.partial,
                        "reference": result.reference,
                    },
                )
            )
    metadata_payload["context_tiers"] = _context_tier_metadata(segments)
    metadata_payload["context_tier_policy"] = {
        "version": 1,
        "protected_tiers": ["instruction", "workspace", "task"],
        "compaction_target": "recent",
    }
    context_token_count = estimate_provider_context_tokens(
        tuple(segments),
        tokenizer_model=policy.tokenizer_model if policy is not None else None,
    )
    metadata_payload["estimated_context_tokens"] = context_token_count.tokens
    metadata_payload["estimated_context_token_source"] = context_token_count.source
    metadata_payload["estimated_context_token_exact"] = context_token_count.exact
    return RuntimeAssembledContext(
        prompt=prompt,
        tool_results=context_window.tool_results,
        continuity_state=continuity_state,
        segments=tuple(segments),
        metadata=metadata_payload,
        loaded_skills=loaded_skills,
    )


def _add_prompt_cache_metadata(
    metadata: dict[str, object],
    assembly_plan: PromptAssemblyPlan,
) -> None:
    """Expose deterministic prompt partitions for provider-side cache keys."""
    sections = assembly_plan.sections
    contents = [section.content for section in sections]
    boundary = dynamic_boundary_marker()
    try:
        boundary_index = contents.index(boundary)
    except ValueError:
        metadata["prompt_cache"] = {"version": 1, "boundary_present": False}
        return
    stable = "\n".join(contents[: boundary_index + 1]).encode("utf-8")
    dynamic = "\n".join(contents[boundary_index + 1 :]).encode("utf-8")
    metadata["prompt_cache"] = {
        "version": 1,
        "boundary_present": True,
        "stable_prefix_hash": hashlib.sha256(stable).hexdigest(),
        "dynamic_suffix_hash": hashlib.sha256(dynamic).hexdigest(),
        "stable_section_count": boundary_index + 1,
        "dynamic_section_count": len(contents) - boundary_index - 1,
    }
