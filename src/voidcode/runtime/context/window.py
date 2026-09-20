from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, cast

from ...agent.prompt_sections import dynamic_boundary_marker
from ...tools.contracts import ToolDiagnostics, ToolResult, ToolResultStatus
from ..todos import render_provider_todo_state
from .prompt_assembly import (
    PromptAssemblyPlan,
    PromptAssemblySection,
    build_prompt_assembly_plan,
    prompt_activation_decision,
)
from .transforms import (
    RuntimeContextTransformResult,
    build_provider_context_transform_result,
)

_CONTINUITY_OBJECTIVE_PREVIEW_CHARS = 160


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
    diagnostics: dict[str, object] | None = None
    truncated: bool = False
    partial: bool = False

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"tool_name": self.tool_name, "status": self.status, "index": self.index}
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
        if self.diagnostics is not None:
            payload["diagnostics"] = dict(self.diagnostics)
        if self.truncated:
            payload["truncated"] = True
        if self.partial:
            payload["partial"] = True
        return payload


@dataclass(frozen=True, slots=True)
class ContextProjection:
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
    dropped_tool_results: tuple[DroppedToolResultDiagnostic, ...] = ()
    version: int = 4

    def metadata_payload(self) -> dict[str, object]:
        return {
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
            "dropped_tool_results": [item.metadata_payload() for item in self.dropped_tool_results],
        }


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    # Whole-context compaction is not performed without authoritative provider
    # usage. This cap is an explicit character limit for one tool payload.
    default_tool_result_chars: int | None = 6_000
    per_tool_result_chars: Mapping[str, int] = field(default_factory=_empty_tool_limits)
    summary_strategy: Literal["deterministic", "model_assisted"] = "deterministic"

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_tool_result_chars", dict(self.per_tool_result_chars))
        if self.default_tool_result_chars is not None and self.default_tool_result_chars < 1:
            raise ValueError("default_tool_result_chars must be >= 1 when provided")
        for tool_name, limit in self.per_tool_result_chars.items():
            if not tool_name:
                raise ValueError("per_tool_result_chars tool names must be non-empty")
            if limit < 1:
                raise ValueError("per_tool_result_chars limits must be >= 1")

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"version": 1, "summary_strategy": self.summary_strategy}
        if self.default_tool_result_chars is not None:
            payload["default_tool_result_chars"] = self.default_tool_result_chars
        if self.per_tool_result_chars:
            payload["per_tool_result_chars"] = dict(self.per_tool_result_chars)
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeContextWindow:
    prompt: str
    tool_results: tuple[ToolResult | ToolResultView, ...] = ()
    compacted: bool = False
    compaction_reason: str | None = None
    original_tool_result_count: int = 0
    retained_tool_result_count: int = 0
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
    """Provider-facing rendering view of a tool result."""

    result: ToolResult
    content: str | None
    clipped: bool = False
    original_content_chars: int | None = None
    content_char_limit: int | None = None
    _isolated_data: dict[str, object] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        isolated_result = deepcopy(self.result)
        object.__setattr__(self, "result", isolated_result)
        object.__setattr__(self, "_isolated_data", deepcopy(isolated_result.data))

    @property
    def tool_name(self) -> str:
        return self.result.tool_name

    @property
    def status(self) -> ToolResultStatus:
        return self.result.status

    @property
    def data(self) -> dict[str, object]:
        return self._isolated_data

    @property
    def error(self) -> str | None:
        return self.result.error

    @property
    def truncated(self) -> bool:
        return self.clipped or self.result.truncated

    @property
    def partial(self) -> bool:
        return True if self.clipped else self.result.partial

    @property
    def reference(self) -> str | None:
        return self.result.reference

    @property
    def diagnostics(self) -> ToolDiagnostics | None:
        return self.result.diagnostics

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
        if not isinstance(tool_name, str) or not tool_name or not isinstance(status, str) or not status:
            continue
        if not isinstance(index, int) or isinstance(index, bool):
            continue
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
                diagnostics=(cast(dict[str, object], entry["diagnostics"]) if isinstance(entry.get("diagnostics"), dict) else None),
                truncated=entry.get("truncated") is True,
                partial=entry.get("partial") is True,
            )
        )
    return tuple(diagnostics)


def continuity_state_from_metadata_payload(
    payload: Mapping[str, object],
) -> ContextProjection | None:
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 4:
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
    if not isinstance(dropped, int) or isinstance(dropped, bool):
        return None
    if not isinstance(retained, int) or isinstance(retained, bool):
        return None
    if not isinstance(source, str):
        return None
    projection_id = payload.get("projection_id")
    source_event_sequence = payload.get("source_event_sequence")
    source_checkpoint_id = payload.get("source_checkpoint_id")
    if not isinstance(source_event_sequence, int) or isinstance(source_event_sequence, bool):
        source_event_sequence = None
    return ContextProjection(
        projection_id=projection_id if isinstance(projection_id, str) else None,
        source_event_sequence=source_event_sequence,
        source_checkpoint_id=source_checkpoint_id if isinstance(source_checkpoint_id, str) else None,
        summary_text=summary_text,
        objective=objective,
        files_changed=_metadata_string_tuple(payload, "files_changed"),
        verbatim_user_constraints=_metadata_string_tuple(payload, "verbatim_user_constraints"),
        progress_completed=_metadata_string_tuple(payload, "progress_completed"),
        blockers_open_questions=_metadata_string_tuple(payload, "blockers_open_questions"),
        key_decisions=_metadata_string_tuple(payload, "key_decisions"),
        relevant_files_commands_errors=_metadata_string_tuple(payload, "relevant_files_commands_errors"),
        verification_state=_metadata_string_tuple(payload, "verification_state"),
        delegated_task_summaries=_metadata_string_tuple(payload, "delegated_task_summaries"),
        recent_tail=_metadata_string_tuple(payload, "recent_tail"),
        dropped_tool_result_count=dropped,
        retained_tool_result_count=retained,
        source=source,
        source_references=_metadata_string_tuple(payload, "source_references"),
        dropped_tool_results=_dropped_tool_diagnostics_from_metadata_payload(payload),
        version=version,
    )


def _previous_continuity_state(
    session_metadata: Mapping[str, object],
) -> ContextProjection | None:
    # Resolve through metadata helpers lazily to keep the module graph acyclic.
    from ..session_metadata_helpers import (
        parse_runtime_state_metadata,
        runtime_state_context_projection,
        runtime_state_context_projection_summary,
    )

    raw_runtime_state = session_metadata.get("runtime_state")
    if raw_runtime_state is None:
        return None
    parse_runtime_state_metadata(raw_runtime_state)
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
        if result.tool_name == "todo":
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
        if result.tool_name in {"task", "background_task"}:
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


def _provider_continuity_summary(summary_text: str, *, prompt: str) -> str:
    """Drop a redundant objective from the provider view only.

    The persisted projection remains the source of truth.  When its rendered
    objective is exactly the same deterministic preview derived from the
    current prompt, repeating it in the provider context adds no information.
    """
    if not prompt.strip():
        return summary_text
    current_preview = _line_preview(prompt, limit=_CONTINUITY_OBJECTIVE_PREVIEW_CHARS)
    objective_prefix = "## Objective\n"
    sections = summary_text.split("\n\n")
    retained: list[str] = []
    for section in sections:
        if section.startswith(objective_prefix) and section[len(objective_prefix) :].strip() == current_preview:
            continue
        retained.append(section)
    return "\n\n".join(retained).strip()


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
                artifact_byte_count=_artifact_metadata_int(result, "byte_count") or _optional_tool_int(result, "original_byte_count"),
                artifact_line_count=_artifact_metadata_int(result, "line_count") or _optional_tool_int(result, "original_line_count"),
                reference=result.reference,
                path=_optional_tool_string(result, "path"),
                command=_optional_tool_string(result, "command"),
                diagnostics=(result.diagnostics.as_payload() if result.diagnostics is not None else None),
                truncated=result.truncated,
                partial=result.partial,
            )
        )
    return tuple(diagnostics)


def _select_recent_tool_result_indexes(results: Sequence[ToolResult | ToolResultView]) -> tuple[int, ...]:
    return tuple(range(len(results)))


def _tool_limit_for_result(result: ToolResult | ToolResultView, policy: ContextWindowPolicy) -> int | None:
    return policy.per_tool_result_chars.get(result.tool_name, policy.default_tool_result_chars)


def _clip_text_to_char_limit(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    marker = f"\n[Tool output truncated by character limit; omitted {omitted} chars]"
    if len(marker) >= limit:
        return marker[:limit]
    return f"{text[: limit - len(marker)]}{marker}"


def _bounded_replayed_conversation_segments(
    segments: tuple[RuntimeContextSegment, ...],
    *,
    policy: ContextWindowPolicy,
) -> tuple[RuntimeContextSegment, ...]:
    bounded: list[RuntimeContextSegment] = []
    for segment in segments:
        if segment.role != "tool" or segment.content is None or segment.tool_name is None:
            bounded.append(segment)
            continue
        metadata = segment.metadata or {}
        if metadata.get("source") != "replayed_conversation":
            bounded.append(segment)
            continue
        limit = policy.per_tool_result_chars.get(segment.tool_name, policy.default_tool_result_chars)
        if limit is None:
            bounded.append(segment)
            continue
        clipped = _clip_text_to_char_limit(segment.content, limit=limit)
        if clipped == segment.content:
            bounded.append(segment)
            continue
        bounded.append(replace(segment, content=clipped, metadata={**metadata, "truncated": True, "partial": True, "char_limit": limit}))
    return tuple(bounded)


def _truncated_view_for_result(
    result: ToolResult | ToolResultView,
    *,
    limit: int | None,
) -> tuple[ToolResultView, bool]:
    if isinstance(result, ToolResultView):
        return result, False
    if limit is None or result.content is None:
        return ToolResultView(result=result, content=result.content), False
    if len(result.content) <= limit:
        return ToolResultView(result=result, content=result.content), False
    clipped = _clip_text_to_char_limit(result.content, limit=limit)
    return (
        ToolResultView(
            result=result,
            content=clipped,
            clipped=True,
            original_content_chars=len(result.content),
            content_char_limit=limit,
        ),
        True,
    )


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
) -> ContextProjection:
    dropped_count = len(dropped_results)
    previewable_dropped_results = tuple(result for result in dropped_results if result.tool_name != "todo")
    previous = _previous_continuity_state(session_metadata)
    objective = previous.objective if previous is not None else None
    if objective is None:
        objective = _line_preview(prompt, limit=_CONTINUITY_OBJECTIVE_PREVIEW_CHARS) if prompt.strip() else None
    progress, blockers, refs, delegated = _facts_from_tool_results(
        previewable_dropped_results,
        preview_item_limit=preview_item_limit,
        preview_char_limit=preview_char_limit,
    )
    retained_tail = tuple(_tool_result_preview(result, max_preview_chars=preview_char_limit) for result in retained_results[-preview_item_limit:])
    previous_constraints = previous.verbatim_user_constraints if previous is not None else ()
    constraints = _merge_unique_strings(previous_constraints, _constraint_lines(prompt), limit=12)
    previous_progress = previous.progress_completed if previous is not None else ()
    previous_blockers = previous.blockers_open_questions if previous is not None else ()
    previous_decisions = previous.key_decisions if previous is not None else ()
    previous_refs = previous.relevant_files_commands_errors if previous is not None else ()
    previous_verification = previous.verification_state if previous is not None else ()
    previous_delegated = previous.delegated_task_summaries if previous is not None else ()
    previous_tail = previous.recent_tail if previous is not None else ()
    state = ContextProjection(
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
        dropped_tool_results=_dropped_tool_diagnostics(dropped_results, original_indexes=dropped_result_indexes),
        source_references=previous.source_references if previous is not None else (),
    )
    if dropped_count == 0:
        return replace(
            state,
            progress_completed=previous_progress,
            blockers_open_questions=previous_blockers,
            recent_tail=_merge_unique_strings(retained_tail, previous_tail, limit=8),
            dropped_tool_results=previous.dropped_tool_results if previous is not None else (),
        )
    dropped_preview = [f"Compacted {dropped_count} earlier tool results:"]
    for index, result in enumerate(previewable_dropped_results[:preview_item_limit], start=1):
        dropped_preview.append(f"{index}. {_tool_result_preview(result, max_preview_chars=preview_char_limit)}")
    remaining = len(previewable_dropped_results) - min(preview_item_limit, len(previewable_dropped_results))
    if remaining > 0:
        dropped_preview.append(f"... and {remaining} more")
    summary = _continuity_summary_text(state)
    return replace(state, summary_text=f"{summary}\n\n## Dropped Tool Preview\n{chr(10).join(dropped_preview)}")


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
    # Resolve through the metadata helper lazily to keep the module graph acyclic.
    from ..session_metadata_helpers import parse_plan_state_metadata

    raw_plan_state = session_metadata.get("plan_state")
    if raw_plan_state is None:
        return None
    plan_state = parse_plan_state_metadata(raw_plan_state)
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
    prepared_results: list[ToolResultView] = []
    truncated_count = 0
    for result in tool_results:
        prepared_result, was_truncated = _truncated_view_for_result(result, limit=_tool_limit_for_result(result, policy))
        prepared_results.append(prepared_result)
        truncated_count += int(was_truncated)
    indexes = _select_recent_tool_result_indexes(prepared_results)
    retained_results = tuple(prepared_results)
    return ToolResultProjection(
        prepared_results=tuple(prepared_results),
        retained_indexes=indexes,
        dropped_indexes=(),
        retained_results=retained_results,
        dropped_results=(),
        truncated_count=truncated_count,
    )


def prepare_provider_context(
    *,
    prompt: str,
    tool_results: tuple[ToolResult | ToolResultView, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
    summary_projector: Callable[[Mapping[str, object]], str] | None = None,
) -> RuntimeContextWindow:
    _ = session_metadata, summary_projector
    effective_policy = policy or ContextWindowPolicy()
    projection = project_tool_results_for_context_window(tool_results=tool_results, policy=effective_policy)
    return RuntimeContextWindow(
        prompt=prompt,
        tool_results=projection.retained_results,
        compacted=False,
        compaction_reason=None,
        original_tool_result_count=len(tool_results),
        retained_tool_result_count=len(projection.retained_results),
        truncated_tool_result_count=projection.truncated_count,
        summary_strategy="deterministic",
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
    workspace: Path | None = None,
    replay_retained_tool_messages: bool = True,
    replayed_conversation_segments: tuple[RuntimeContextSegment, ...] = (),
    summary_projector: Callable[[Mapping[str, object]], str] | None = None,
    tool_catalog_context: str = "",
) -> RuntimeAssembledContext:
    context_window = prepare_provider_context(
        prompt=prompt,
        tool_results=tool_results,
        session_metadata=session_metadata,
        policy=policy,
        summary_projector=summary_projector,
    )
    effective_policy = policy or ContextWindowPolicy()
    replayed_conversation_segments = _bounded_replayed_conversation_segments(
        replayed_conversation_segments,
        policy=effective_policy,
    )
    transform_result = context_transform_result or build_provider_context_transform_result(
        workspace=workspace,
        tool_results=tool_results,
        hook_preset_context=hook_preset_context,
        rulebook_snapshot=session_metadata.get("rulebook_snapshot"),
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
            provider_summary = _provider_continuity_summary(summary_text.strip(), prompt=prompt)
            if provider_summary:
                continuity_summary = f"Runtime context projection:\n{provider_summary}"
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
        continuity_summary=continuity_summary,
        artifact_reference_sections=artifact_reference_sections,
        prompt_profile_name=prompt_profile_name,
        prompt_activation_section=activation_decision.section,
        tool_catalog_context=tool_catalog_context,
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
            if todo_prompt_context is not None and result.tool_name == "todo":
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
