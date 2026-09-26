from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from ...agent.prompt_sections import dynamic_boundary_marker
from ...hook.percall import PerCallRewriteOutcome
from ...tools.contracts import ToolDiagnostics, ToolResult, ToolResultStatus
from ..todos import render_provider_todo_state
from .percall import (
    percall_wire_cache_prefix,
    segments_to_percall_messages,
)
from .projection import project_summary
from .prompt_assembly import (
    PromptAssemblyPlan,
    PromptAssemblySection,
    build_prompt_assembly_plan,
    is_context_tier,
    metadata_source,
    prompt_activation_decision,
)
from .transforms import (
    RuntimeContextTransformResult,
    build_provider_context_transform_result,
)

if TYPE_CHECKING:
    from ..config import RuntimeCompactionConfig

_CONTINUITY_OBJECTIVE_PREVIEW_CHARS = 160
_COMPACTION_PREVIEW_ITEM_LIMIT = 8
_COMPACTION_PREVIEW_CHAR_LIMIT = 240

#: Token accounting labels and estimator ratios (UTF-8 bytes / 4).
_TOKEN_ESTIMATE_BYTES_PER_TOKEN = 4
_TOKEN_RESERVE_NUMERATOR = 15
_TOKEN_RESERVE_DENOMINATOR = 100
#: Reserve floor for the compaction threshold (upstream: ``max(15% of window, 16384)``).
DEFAULT_CONTEXT_RESERVE_TOKENS = 16_384

#: Default bounded-pruning knobs and floors: the knobs are overridable by
#: ``context_window.compaction``, the two floors are fixed production constants.
DEFAULT_KEEP_RECENT_TOOL_TOKENS = 20_000
DEFAULT_MIN_SAVINGS_TOKENS = 20_000
DEFAULT_MIN_PRUNE_TOKENS = 50

#: Tool results whose content is never replaced by a pruning placeholder: the
#: plan surface (todo) and skill/rule bodies carry instructions the model must
#: keep verbatim, and a placeholder would silently drop them.
_PRUNE_PROTECTED_TOOL_NAMES: Final = frozenset({"todo", "skill"})
#: Rule/rulebook reads go through the ``voidcode://rule/<name>`` internal URL
#: (``tools/read.py::RULE_URI_PREFIX``), so that is the scheme worth protecting.
#: Artifacts are deliberately *not* protected: pruning is what points the model at
#: them.
_PRUNE_PROTECTED_PATH_PREFIXES: Final = ("voidcode://rule/",)
_PRUNE_PROTECTED_PATH_PARTS: Final = (".voidcode/rules",)
#: Scalar ``data`` values longer than this are dropped from a pruned result.
_PRUNE_SCALAR_DATA_CHARS = 256


def _empty_tool_limits() -> dict[str, int]:
    return {}


def _default_compaction_config() -> RuntimeCompactionConfig:
    # Local import: ``runtime.config`` imports this module's defaults (cycle).
    from ..config import RuntimeCompactionConfig

    return RuntimeCompactionConfig()


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
    # Per-result bound: this cap is an explicit character limit for one tool
    # payload. Whole-context pruning is token-budget driven (``compaction``),
    # never character driven.
    default_tool_result_chars: int | None = 6_000
    per_tool_result_chars: Mapping[str, int] = field(default_factory=_empty_tool_limits)
    summary_strategy: Literal["deterministic", "model_assisted"] = "deterministic"
    #: Bounded pruning knobs (single representation; the floors are production constants).
    compaction: RuntimeCompactionConfig = field(default_factory=_default_compaction_config)

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
        payload: dict[str, object] = {
            "version": 1,
            "summary_strategy": self.summary_strategy,
            "compaction": asdict(self.compaction),
        }
        if self.default_tool_result_chars is not None:
            payload["default_tool_result_chars"] = self.default_tool_result_chars
        if self.per_tool_result_chars:
            payload["per_tool_result_chars"] = dict(self.per_tool_result_chars)
        return payload


@dataclass(frozen=True, slots=True)
class BeforeCompactInput:
    """Thin cancellable input consulted before compacting; engine untouched."""

    cancel: bool = False
    reason: str | None = None
    custom_summary: str | None = None
    extra_context: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeContextWindow:
    prompt: str
    tool_results: tuple[ToolResult | ToolResultView, ...] = ()
    compacted: bool = False
    compaction_reason: str | None = None
    original_tool_result_count: int = 0
    retained_tool_result_count: int = 0
    truncated_tool_result_count: int = 0
    #: Results whose content was replaced by a bounded pruning placeholder; the
    #: message/tool pairing is preserved, so ``original == retained + dropped``.
    dropped_tool_result_count: int = 0
    #: Decision token numbers before/after pruning: the measured provider anchor
    #: plus an estimated increment (UTF-8 bytes / 4). ``None`` when the call
    #: could not size the full payload.
    usage_tokens_before: int | None = None
    usage_tokens_after: int | None = None
    #: Token accounting provenance: ``usage_tokens_*`` are the decision numbers
    #: (anchor + estimated increment when the provider reported usage); these
    #: fields say which part was measured and which was estimated.
    measured_anchor_tokens: int | None = None
    estimated_delta_tokens: int | None = None
    pruned_savings_tokens: int = 0
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
        if self.dropped_tool_result_count:
            payload["dropped_tool_result_count"] = self.dropped_tool_result_count
        if self.usage_tokens_before is not None:
            payload["usage_tokens_before"] = self.usage_tokens_before
            payload["usage_tokens_after"] = self.usage_tokens_after
            payload["usage_tokens_estimated"] = True
        # Provenance is always reported: a consumer must be able to tell a
        # measured anchor from a pure estimate even when the call could not size
        # the full payload (``usage_tokens_before`` absent).
        payload["measured_anchor_tokens"] = self.measured_anchor_tokens
        payload["estimated_delta_tokens"] = self.estimated_delta_tokens
        if self.pruned_savings_tokens:
            payload["pruned_savings_tokens"] = self.pruned_savings_tokens
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
    #: True when the budget replaced the content with a pruning placeholder.
    pruned: bool = False
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
        return self.pruned or self.clipped or self.result.truncated

    @property
    def partial(self) -> bool:
        return True if self.pruned or self.clipped else self.result.partial

    @property
    def reference(self) -> str | None:
        return self.result.reference

    @property
    def source(self) -> str | None:
        return self.result.source

    @property
    def diagnostics(self) -> ToolDiagnostics | None:
        return self.result.diagnostics

    def __getattr__(self, name: str) -> object:
        return getattr(self.result, name)


@dataclass(frozen=True, slots=True)
class ToolResultProjection:
    #: Provider-facing views, in provider order (pairing preserved; content may
    #: already carry char-cap clipping).
    retained_results: tuple[ToolResultView, ...]
    truncated_count: int


@dataclass(frozen=True, slots=True)
class RuntimeAssembledContext:
    prompt: str
    tool_results: tuple[ToolResult | ToolResultView, ...]
    continuity_state: ContextProjection | None
    segments: tuple[RuntimeContextSegment, ...]
    metadata: dict[str, object]
    loaded_skills: tuple[dict[str, object], ...] = ()
    #: The bounded provider view this assembly was rendered from: the compiled
    #: window owns the honest compaction counts for the segments above.
    context_window: RuntimeContextWindow | None = None


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
        tier = metadata.get("tier")
        if not is_context_tier(tier):
            continue
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
        tool_call_id = _optional_tool_string_or_none(result, "tool_call_id")
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
    raw_items = raw
    values: list[str] = []
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
    return tuple(values)


def _optional_entry_string_or_none(entry: Mapping[str, object], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) and value else None


def _optional_entry_int_or_none(entry: Mapping[str, object], key: str) -> int | None:
    value = entry.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dropped_tool_diagnostics_from_metadata_payload(
    payload: Mapping[str, object],
) -> tuple[DroppedToolResultDiagnostic, ...]:
    raw = payload.get("dropped_tool_results")
    if not isinstance(raw, list | tuple):
        return ()
    diagnostics: list[DroppedToolResultDiagnostic] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = item
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
                tool_call_id=_optional_entry_string_or_none(entry, "tool_call_id"),
                artifact_id=_optional_entry_string_or_none(entry, "artifact_id"),
                artifact_status=_optional_entry_string_or_none(entry, "artifact_status"),
                artifact_byte_count=_optional_entry_int_or_none(entry, "artifact_byte_count"),
                artifact_line_count=_optional_entry_int_or_none(entry, "artifact_line_count"),
                reference=_optional_entry_string_or_none(entry, "reference"),
                path=_optional_entry_string_or_none(entry, "path"),
                command=_optional_entry_string_or_none(entry, "command"),
                diagnostics=(entry["diagnostics"] if isinstance(entry.get("diagnostics"), dict) else None),
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


def _optional_tool_string_or_none(result: ToolResult | ToolResultView, key: str) -> str | None:
    value = result.data.get(key)
    return value if isinstance(value, str) and value else None


def _optional_tool_int_or_none(result: ToolResult | ToolResultView, key: str) -> int | None:
    value = result.data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _artifact_metadata_value(result: ToolResult | ToolResultView, key: str) -> object:
    artifact = result.data.get("artifact")
    if isinstance(artifact, Mapping):
        value = artifact.get(key)
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
                tool_call_id=_optional_tool_string_or_none(result, "tool_call_id"),
                artifact_id=_artifact_metadata_string(result, "artifact_id"),
                artifact_status=_artifact_metadata_string(result, "status") or _optional_tool_string_or_none(result, "artifact_status"),
                artifact_byte_count=_artifact_metadata_int(result, "byte_count") or _optional_tool_int_or_none(result, "original_byte_count"),
                artifact_line_count=_artifact_metadata_int(result, "line_count") or _optional_tool_int_or_none(result, "original_line_count"),
                reference=result.reference,
                path=_optional_tool_string_or_none(result, "path"),
                command=_optional_tool_string_or_none(result, "command"),
                diagnostics=(result.diagnostics.as_payload() if result.diagnostics is not None else None),
                truncated=result.truncated,
                partial=result.partial,
            )
        )
    return tuple(diagnostics)


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


def _result_data_bytes(result: ToolResult | ToolResultView) -> int:
    """UTF-8 bytes of the result's ``data`` payload the provider receives.

    Tool results carry their body in ``data`` (a read result's ``lines`` /
    ``raw_content``), so a content-only estimate under-counts the real request.
    ``json.dumps`` mirrors the adapters' wire encoding.
    """
    data = result.data
    if not data:
        return 0
    try:
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError, ValueError:
        return 0
    return len(encoded.encode("utf-8"))


def _result_payload_bytes(result: ToolResult | ToolResultView) -> int:
    """Provider-visible UTF-8 bytes of one tool result (content plus its data payload)."""
    return len((result.content or "").encode("utf-8")) + _result_data_bytes(result)


def pruning_data_payload(result: ToolResult | ToolResultView, *, omitted_bytes: int) -> dict[str, object]:
    """Bounded replacement for a pruned result's ``data``.

    Scalars survive (path/status/line counts stay readable); containers and long
    bodies are dropped, so the placeholder costs dozens of chars instead of the
    whole payload while the value stays a JSON object (adapter wire shape
    unchanged).
    """
    payload: dict[str, object] = {"context_pruned": True, "omitted_payload_bytes": omitted_bytes}
    for key, value in result.data.items():
        if isinstance(value, (dict, list, tuple, set)):
            continue
        if isinstance(value, str) and len(value) > _PRUNE_SCALAR_DATA_CHARS:
            continue
        payload[key] = value
    return payload


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _pruned_view_count(results: Sequence[ToolResult | ToolResultView]) -> int:
    """How many views in a provider view already carry a pruning placeholder."""
    return sum(1 for result in results if isinstance(result, ToolResultView) and result.pruned)


def _is_prune_protected(result: ToolResult | ToolResultView) -> bool:
    """Whether a result's content must survive pruning verbatim."""
    if result.tool_name in _PRUNE_PROTECTED_TOOL_NAMES:
        return True
    raw_path = result.data.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return False
    if raw_path.startswith(_PRUNE_PROTECTED_PATH_PREFIXES):
        return True
    return any(part in raw_path for part in _PRUNE_PROTECTED_PATH_PARTS)


def pruning_placeholder(result: ToolResult | ToolResultView, *, omitted_bytes: int, omitted_tokens: int) -> str:
    """Bounded placeholder that states the omitted scale (and how to recover it)."""
    artifact_id = _artifact_metadata_string(result, "artifact_id")
    parts = [
        f"[Runtime context pruning: {result.tool_name} result content omitted;",
        f"omitted_bytes={omitted_bytes};",
        f"estimated_omitted_tokens={omitted_tokens};",
        f"status={result.status};",
        "message and tool pairing preserved.",
    ]
    if artifact_id is not None:
        parts.append(f"artifact_id={artifact_id};")
        parts.append(f'read(path="voidcode://artifact/{artifact_id}") recovers the full output.')
    parts.append("]")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class PrunedToolResults:
    """Outcome of bounded content pruning over one provider view."""

    rendered_results: tuple[ToolResultView, ...]
    #: Pre-placeholder views of the pruned results, in provider order.
    pruned_views: tuple[ToolResultView, ...]
    pruned_indexes: tuple[int, ...]
    saved_tokens: int = 0


def prune_tool_results_for_budget(
    results: tuple[ToolResultView, ...],
    *,
    target_tokens: int,
    min_savings_tokens: int,
    min_prune_tokens: int,
) -> PrunedToolResults:
    """Replace the oldest prunable tool content with placeholders until it fits ``target_tokens``.

    Deterministic: the outcome depends only on the result order, the budget and
    the protection set. Results below ``min_prune_tokens`` and protected results
    are left verbatim; when the reclaimed estimate is under
    ``min_savings_tokens`` nothing is pruned at all, so a marginal overage never
    rewrites the view.
    """
    remaining = sum(estimate_tokens_for_bytes(_result_payload_bytes(result)) for result in results)
    if remaining <= target_tokens:
        return PrunedToolResults(rendered_results=results, pruned_views=(), pruned_indexes=())
    rendered = list(results)
    pruned_views: list[ToolResultView] = []
    pruned_indexes: list[int] = []
    saved_tokens = 0
    for index, view in enumerate(results):
        if remaining <= target_tokens:
            break
        content = view.content or ""
        payload_bytes = _result_payload_bytes(view)
        payload_tokens = estimate_tokens_for_bytes(payload_bytes)
        if payload_tokens < min_prune_tokens or _is_prune_protected(view):
            continue
        placeholder = pruning_placeholder(view, omitted_bytes=payload_bytes, omitted_tokens=payload_tokens)
        pruned_data = pruning_data_payload(view, omitted_bytes=_result_data_bytes(view)) if view.data else view.data
        placeholder_bytes = len(placeholder.encode("utf-8")) + len(
            json.dumps(pruned_data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        )
        reclaimed = payload_tokens - estimate_tokens_for_bytes(placeholder_bytes)
        if reclaimed <= 0:
            continue
        rendered[index] = replace(
            view,
            result=replace(view.result, data=pruned_data),
            content=placeholder,
            pruned=True,
            original_content_chars=len(content),
            content_char_limit=None,
        )
        pruned_views.append(view)
        pruned_indexes.append(index)
        saved_tokens += reclaimed
        remaining -= reclaimed
    if saved_tokens < min_savings_tokens:
        return PrunedToolResults(rendered_results=results, pruned_views=(), pruned_indexes=())
    return PrunedToolResults(
        rendered_results=tuple(rendered),
        pruned_views=tuple(pruned_views),
        pruned_indexes=tuple(pruned_indexes),
        saved_tokens=saved_tokens,
    )


@dataclass(frozen=True, slots=True)
class CompactionBudget:
    """Budget inputs that authorize bounded pruning for one provider call.

    The runtime resolves these from the effective config (catalog window plus the
    ``context_window.compaction`` group); ``None`` means "not sized", which leaves
    pruning to a caller that can size the whole request.
    """

    context_window: int | None = None
    threshold_tokens: int | None = None
    reserve_tokens: int | None = None
    #: Recovery target: prune until the *whole* provider view fits the threshold
    #: (catalog window minus reserve), not merely until the tool content fits
    #: ``keep_recent_tool_tokens``. Set by the runtime's context-limit recovery.
    fit_payload: bool = False
    #: Last provider-reported context size (``provider_usage.latest``); when
    #: present the budget decision is anchor + estimated increment instead of a
    #: pure estimate. ``None`` means no usable report.
    anchor_tokens: int | None = None


def _continuity_provider_sections(
    continuity_state: ContextProjection | None,
    *,
    prompt: str,
) -> tuple[str, tuple[PromptAssemblySection, ...]]:
    """Render ``(continuity summary, artifact reference sections)`` for a plan build."""
    if continuity_state is None:
        return "", ()
    summary = ""
    summary_text = continuity_state.summary_text
    if isinstance(summary_text, str) and summary_text.strip():
        provider_summary = _provider_continuity_summary(summary_text.strip(), prompt=prompt)
        if provider_summary:
            summary = f"Runtime context projection:\n{provider_summary}"
    return (
        summary,
        tuple(
            PromptAssemblySection(
                role=segment.role,
                content=segment.content or "",
                source=metadata_source(segment.metadata or {}, fallback="runtime_context_artifact_reference"),
                tier="recent",
                metadata={} if segment.metadata is None else dict(segment.metadata),
            )
            for segment in _artifact_reference_segments(continuity_state)
        ),
    )


def _provider_payload_bytes(
    plan: PromptAssemblyPlan,
    *,
    replayed_conversation_segments: tuple[RuntimeContextSegment, ...],
) -> int:
    """Chars the provider sees outside the current prompt and the tool results.

    The prompt is excluded because :func:`prepare_provider_context` adds it
    itself; the tool results are excluded because they are the content the budget
    decision actually bounds.
    """
    plan_bytes = sum(len(section.content.encode("utf-8")) for section in plan.sections if section.source != "current_user_prompt")
    replay_bytes = sum(len((segment.content or "").encode("utf-8")) for segment in replayed_conversation_segments)
    return plan_bytes + replay_bytes


def project_tool_results_for_context_window(
    *,
    tool_results: tuple[ToolResult | ToolResultView, ...],
    policy: ContextWindowPolicy,
) -> ToolResultProjection:
    """Char-cap every result into its provider-facing view (no pruning decision).

    Budget pruning is a separate, explicitly budgeted step
    (:func:`prune_tool_results_for_budget`); this projection only bounds each
    individual payload.
    """
    prepared_results: list[ToolResultView] = []
    truncated_count = 0
    for result in tool_results:
        prepared_result, was_truncated = _truncated_view_for_result(result, limit=_tool_limit_for_result(result, policy))
        prepared_results.append(prepared_result)
        truncated_count += int(was_truncated)
    return ToolResultProjection(
        retained_results=tuple(prepared_results),
        truncated_count=truncated_count,
    )


def prepare_provider_context(
    *,
    prompt: str,
    tool_results: tuple[ToolResult | ToolResultView, ...],
    session_metadata: dict[str, object],
    policy: ContextWindowPolicy | None = None,
    summary_projector: Callable[[Mapping[str, object]], str] | None = None,
    context_window: int | None = None,
    threshold_tokens: int | None = None,
    reserve_tokens: int | None = None,
    compaction_enabled: bool = True,
    before_compact: BeforeCompactInput | None = None,
    payload_bytes: int | None = None,
    fit_payload: bool = False,
    anchor_tokens: int | None = None,
) -> RuntimeContextWindow:
    """Compile the bounded provider view for one call.

    ``payload_bytes`` sizes everything else the provider sees on this call
    (instruction/system sections, replayed conversation, transform injections);
    it is required to authorize a pruning decision, because a token estimate that
    ignores those sections cannot know whether the request fits. ``None`` means
    the caller cannot size the full payload: this call then only applies
    per-result char caps and leaves pruning to the payload-aware caller
    (:func:`assemble_provider_context`).

    Every token decision number is the measured provider anchor plus an estimate
    of everything added since: the anchor is the last provider-reported context
    size (``anchor_tokens``), the increment is estimated at UTF-8 bytes / 4, and
    """
    effective_policy = policy or ContextWindowPolicy()
    projection = project_tool_results_for_context_window(tool_results=tool_results, policy=effective_policy)
    measured_anchor_tokens = anchor_tokens if _positive_int(anchor_tokens) else None
    counts: dict[str, int] = {
        "original_tool_result_count": len(tool_results),
        "retained_tool_result_count": len(projection.retained_results),
        "truncated_tool_result_count": projection.truncated_count,
    }

    def _view(
        *,
        results: tuple[ToolResultView, ...],
        compacted: bool,
        reason: str | None,
        dropped: int = 0,
        savings: int = 0,
        usage_before: int | None = None,
        usage_after: int | None = None,
        delta: int | None = None,
        continuity: ContextProjection | None = None,
        summary_anchor: str | None = None,
        summary_source: dict[str, int] | None = None,
        summary_strategy: Literal["deterministic", "model_assisted", "fallback"] = "deterministic",
        fallback_reason: str | None = None,
    ) -> RuntimeContextWindow:
        """One compiled view; every branch differs only in the fields it passes."""
        return RuntimeContextWindow(
            prompt=prompt,
            tool_results=results,
            compacted=compacted,
            compaction_reason=reason,
            **counts,
            dropped_tool_result_count=dropped,
            usage_tokens_before=usage_before,
            usage_tokens_after=usage_after,
            measured_anchor_tokens=measured_anchor_tokens,
            estimated_delta_tokens=delta,
            pruned_savings_tokens=savings,
            continuity_state=continuity,
            summary_anchor=summary_anchor,
            summary_source=summary_source,
            summary_strategy=summary_strategy,
            summary_fallback_reason=fallback_reason,
        )

    if payload_bytes is None:
        return _view(results=projection.retained_results, compacted=False, reason=None)
    delta_tokens = estimate_tokens_for_bytes(
        payload_bytes + len(prompt.encode("utf-8")) + sum(_result_payload_bytes(result) for result in projection.retained_results)
    )
    usage_tokens = (measured_anchor_tokens or 0) + delta_tokens
    threshold = resolve_threshold_tokens(
        context_window,
        threshold_tokens=threshold_tokens,
        reserve_tokens=reserve_tokens,
    )
    if threshold <= 0:
        # Nothing sizes this model's compaction (catalog miss): say so instead of
        # silently returning an unbounded view.
        return _view(
            results=projection.retained_results,
            compacted=False,
            reason=f"compaction_unsized:usage_tokens={usage_tokens}:no_context_window",
            usage_before=usage_tokens,
            usage_after=usage_tokens,
            delta=delta_tokens,
        )
    # ponytail: sync-only seam, no executor calls; custom_summary capped by
    # existing preview/projector limits, add explicit max chars if projector input grows.
    if before_compact is not None and before_compact.cancel:
        return _view(
            results=projection.retained_results,
            compacted=False,
            reason=before_compact.reason,
            usage_before=usage_tokens,
            usage_after=usage_tokens,
            delta=delta_tokens,
        )
    if not should_compact(
        usage_tokens,
        context_window,
        enabled=compaction_enabled and effective_policy.compaction.enabled,
        strategy=effective_policy.summary_strategy,
        threshold_tokens=threshold_tokens,
        reserve_tokens=reserve_tokens,
    ):
        # Under threshold the view stays byte-identical; a view that already
        # carries placeholders (recompiled from an earlier bounded window) still
        # reports them instead of claiming nothing was dropped.
        already_pruned = _pruned_view_count(projection.retained_results)
        return _view(
            results=projection.retained_results,
            compacted=already_pruned > 0,
            reason=(f"already_pruned_view:pruned_tool_results={already_pruned}" if already_pruned else None),
            dropped=already_pruned,
            usage_before=usage_tokens,
            usage_after=usage_tokens,
            delta=delta_tokens,
        )
    prune_target = effective_policy.compaction.keep_recent_tool_tokens
    if fit_payload:
        # Recovery asks for the whole view to fit, so the tool results may only
        # keep whatever the instructions/prompt leave under the threshold.
        non_tool_tokens = estimate_tokens_for_bytes(payload_bytes + len(prompt.encode("utf-8")))
        prune_target = max(0, threshold - non_tool_tokens)
    pruned = prune_tool_results_for_budget(
        projection.retained_results,
        target_tokens=prune_target,
        # The savings floor guards *routine* pruning against churn. Recovery
        # (``fit_payload``) exists because the request did not fit at all, so a
        # small but sufficient reclaim must not be rejected for being small; the
        # per-result ``min_prune_tokens`` floor still applies.
        min_savings_tokens=0 if fit_payload else DEFAULT_MIN_SAVINGS_TOKENS,
        min_prune_tokens=DEFAULT_MIN_PRUNE_TOKENS,
    )
    if not pruned.pruned_indexes:
        return _view(
            results=projection.retained_results,
            compacted=False,
            reason=(
                "token_budget_exceeded:no_prunable_tool_content:"
                f"usage_tokens={usage_tokens}:threshold_tokens={threshold}:keep_recent_tool_tokens={effective_policy.compaction.keep_recent_tool_tokens}"
            ),
            usage_before=usage_tokens,
            usage_after=usage_tokens,
            delta=delta_tokens,
        )
    delta_tokens_after = estimate_tokens_for_bytes(
        payload_bytes + len(prompt.encode("utf-8")) + sum(_result_payload_bytes(result) for result in pruned.rendered_results)
    )
    continuity_state = _build_continuity_state(
        prompt=prompt,
        session_metadata=session_metadata,
        dropped_results=pruned.pruned_views,
        dropped_result_indexes=pruned.pruned_indexes,
        retained_results=pruned.rendered_results,
        retained_count=len(pruned.rendered_results),
        preview_item_limit=_COMPACTION_PREVIEW_ITEM_LIMIT,
        preview_char_limit=_COMPACTION_PREVIEW_CHAR_LIMIT,
    )
    deterministic_summary = _continuity_summary_text(continuity_state)
    summary_facts: dict[str, object] = {
        "prompt": prompt,
        "deterministic_summary": deterministic_summary,
        "dropped_tool_result_count": continuity_state.dropped_tool_result_count,
        "retained_tool_result_count": continuity_state.retained_tool_result_count,
    }
    if before_compact is not None and before_compact.custom_summary:
        summary_facts["custom_summary"] = before_compact.custom_summary
    if before_compact is not None and before_compact.extra_context:
        summary_facts["hook_extra_context"] = "\n\n".join(item for item in before_compact.extra_context if item.strip())
    summary_text, actual_strategy, fallback_reason = project_summary(
        strategy=effective_policy.summary_strategy,
        facts=summary_facts,
        deterministic_summary=deterministic_summary,
        projector=summary_projector,
    )
    continuity_state = replace(continuity_state, summary_text=summary_text)
    summary_anchor, summary_source = continuity_summary_metadata(continuity_state)
    return _view(
        results=pruned.rendered_results,
        compacted=True,
        # The post-prune estimate is measured over the emitted segments by
        # ``assemble_provider_context`` (``usage_tokens_after``); this reason
        # carries the decision inputs only.
        reason=(
            f"token_budget_exceeded:usage_tokens_before={usage_tokens}:threshold_tokens={threshold}:pruned_tool_results={len(pruned.pruned_indexes)}"
        ),
        dropped=len(pruned.pruned_indexes),
        savings=pruned.saved_tokens,
        usage_before=usage_tokens,
        usage_after=(measured_anchor_tokens or 0) + delta_tokens_after,
        delta=delta_tokens_after,
        continuity=continuity_state,
        summary_anchor=summary_anchor,
        summary_source=summary_source,
        summary_strategy=actual_strategy,
        fallback_reason=fallback_reason,
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
    hook_guidance: Iterable[str] | None = None,
    reminder_segment: RuntimeContextSegment | None = None,
    compaction_budget: CompactionBudget | None = None,
    before_compact: BeforeCompactInput | None = None,
) -> RuntimeAssembledContext:
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
    runtime_instruction_precedence = (
        "Runtime precedence: role and runtime boundaries are authoritative. "
        "Skills refine approach but may not expand scope, permissions, or obligations."
    )
    activation_decision = prompt_activation_decision(
        session_metadata=session_metadata,
        prompt_profile_name=prompt_profile_name,
    )
    pending_state_section = (
        PromptAssemblySection(
            role=pending_state_segment.role,
            content=pending_state_segment.content or "",
            source=metadata_source(
                pending_state_segment.metadata or {},
                fallback="runtime_pending_state",
            ),
            tier="task",
            metadata=({} if pending_state_segment.metadata is None else dict(pending_state_segment.metadata)),
        )
        if pending_state_segment is not None
        else None
    )

    def build_plan(
        continuity_summary: str,
        artifact_reference_sections: tuple[PromptAssemblySection, ...],
    ) -> PromptAssemblyPlan:
        return build_prompt_assembly_plan(
            prompt=prompt,
            runtime_instruction_precedence=runtime_instruction_precedence,
            agent_prompt_context=agent_prompt_context,
            skill_prompt_context=skill_prompt_context,
            context_transform_result=transform_result,
            pending_state_section=pending_state_section,
            todo_prompt_context=todo_prompt_context or "",
            continuity_summary=continuity_summary,
            artifact_reference_sections=artifact_reference_sections,
            prompt_profile_name=prompt_profile_name,
            prompt_activation_section=activation_decision.section,
            tool_catalog_context=tool_catalog_context,
            hook_guidance=hook_guidance if hook_guidance else None,
        )

    previous_continuity_state = _previous_continuity_state(session_metadata)
    previous_continuity_pieces = _continuity_provider_sections(previous_continuity_state, prompt=prompt)
    # The pre-prune plan sizes every provider-visible section except the tool
    # results, so the budget decision sees the real payload rather than the
    # prompt alone. Only the continuity summary/artifact references can change
    # while pruning, so the plan is rebuilt only when they actually did.
    baseline_plan = build_plan(*previous_continuity_pieces)
    context_window = prepare_provider_context(
        prompt=prompt,
        tool_results=tool_results,
        session_metadata=session_metadata,
        policy=effective_policy,
        summary_projector=summary_projector,
        context_window=None if compaction_budget is None else compaction_budget.context_window,
        threshold_tokens=None if compaction_budget is None else compaction_budget.threshold_tokens,
        reserve_tokens=None if compaction_budget is None else compaction_budget.reserve_tokens,
        before_compact=before_compact,
        fit_payload=compaction_budget is not None and compaction_budget.fit_payload,
        payload_bytes=(
            None
            if compaction_budget is None
            else _provider_payload_bytes(baseline_plan, replayed_conversation_segments=replayed_conversation_segments)
        ),
        anchor_tokens=None if compaction_budget is None else compaction_budget.anchor_tokens,
    )
    # This turn's pruning outcome outranks the persisted projection: the artifact
    # references and dropped-result facts must describe the view the provider is
    # about to receive, and the persisted projection is only carried forward
    # through ``_build_continuity_state``.
    continuity_state = context_window.continuity_state or preserved_continuity_state or previous_continuity_state
    if continuity_state is not None and continuity_state.projection_id is None:
        anchor, _ = continuity_summary_metadata(continuity_state)
        if anchor is not None:
            continuity_state = replace(continuity_state, projection_id=anchor)
    continuity_pieces = _continuity_provider_sections(continuity_state, prompt=prompt)
    assembly_plan = baseline_plan if continuity_pieces == previous_continuity_pieces else build_plan(*continuity_pieces)
    metadata_payload = context_window.metadata_payload()
    if continuity_state is not None and "projection" not in metadata_payload:
        metadata_payload["projection"] = continuity_state.metadata_payload()
    if continuity_state is not None and "summary_anchor" not in metadata_payload:
        summary_anchor, summary_source = continuity_summary_metadata(continuity_state)
        if summary_anchor is not None:
            metadata_payload["summary_anchor"] = summary_anchor
        if summary_source is not None:
            metadata_payload["summary_source"] = summary_source
    if transform_result.traces:
        metadata_payload["context_transforms"] = transform_result.metadata_payload()
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
            if result.source == "replayed_conversation":
                continue
            raw_tool_call_id = result.data.get("tool_call_id")
            tool_call_id = raw_tool_call_id if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip() else f"voidcode_tool_{index}"
            raw_arguments = result.data.get("arguments")
            tool_arguments: dict[str, object]
            if isinstance(raw_arguments, dict):
                tool_arguments = dict(raw_arguments)
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
            pruned_metadata: dict[str, object] = {}
            if isinstance(result, ToolResultView) and result.pruned:
                pruned_metadata["pruned"] = True
                if result.original_content_chars is not None:
                    pruned_metadata["original_content_chars"] = result.original_content_chars
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
                        **pruned_metadata,
                    },
                )
            )
    if compaction_budget is not None:
        # Measured on the segments the provider will actually receive (the
        # per-call reminder is appended after this and is not part of the
        # compaction decision). Estimated tokens, never provider usage.
        measured_payload_tokens = estimate_tokens_for_bytes(sum(len((segment.content or "").encode("utf-8")) for segment in segments))
        context_window = replace(
            context_window,
            usage_tokens_after=(context_window.measured_anchor_tokens or 0) + measured_payload_tokens,
            estimated_delta_tokens=measured_payload_tokens,
        )
        metadata_payload["usage_tokens_after"] = context_window.usage_tokens_after
        metadata_payload["usage_tokens_estimated"] = True
    if reminder_segment is not None:
        # Tail-appended per-call reminder: reaches the provider for this call
        # only (see ``segments_to_percall_messages``), never the transcript.
        segments.append(reminder_segment)
    metadata_payload["context_tiers"] = _context_tier_metadata(segments)
    metadata_payload["context_tier_policy"] = {
        "version": 1,
        "protected_tiers": ["instruction", "workspace", "task"],
        "compaction_target": "recent",
    }
    percall_outcome = PerCallRewriteOutcome(messages=segments_to_percall_messages(tuple(segments)))
    metadata_payload["percall_cache_prefix"] = percall_wire_cache_prefix(percall_outcome)
    return RuntimeAssembledContext(
        prompt=prompt,
        tool_results=context_window.tool_results,
        continuity_state=continuity_state,
        segments=tuple(segments),
        metadata=metadata_payload,
        loaded_skills=loaded_skills,
        context_window=context_window,
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


# Token estimator seam (deterministic-first, no tokenizer dependency).

#: Local estimator: UTF-8 bytes / 4 (ceil). Matches the upstream pi-agent-core
#: default (`(byteLength(text) + 3) >> 2`), and unlike a per-character ratio it
#: does not under-count non-ASCII payloads (Chinese text is ~3 bytes per char).


def estimate_tokens_for_bytes(
    byte_count: int,
    bytes_per_token: int = _TOKEN_ESTIMATE_BYTES_PER_TOKEN,
) -> int:
    """Ceiling UTF-8-bytes/4 guess; non-positive input estimates to 0."""
    if byte_count <= 0:
        return 0
    if bytes_per_token <= 0:
        raise ValueError("bytes_per_token must be >= 1")
    return -(-byte_count // bytes_per_token)


def provider_usage_anchor_tokens(session_metadata: Mapping[str, object]) -> int | None:
    """Last provider-reported context size, usable as a measured anchor.

    Upstream semantics: ``input + cache_read + cache_write + output`` -- the
    reported input (cache reads included) plus this turn's output, because the
    output becomes history on the next request. voidcode's usage buckets *are*
    the provider's own numbers (there is no separate orchestration bucket to
    subtract), so nothing is deducted here. A missing or all-zero report has no
    anchor to offer and returns ``None``.
    """
    raw_usage = session_metadata.get("provider_usage")
    if not isinstance(raw_usage, dict):
        return None
    raw_latest = raw_usage.get("latest")
    if not isinstance(raw_latest, dict):
        return None
    total = 0
    reported = False
    for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
        value = raw_latest.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
            reported = True
    return total if reported and total > 0 else None


def _reserve_floor(floor: int) -> int:
    return floor if isinstance(floor, int) and not isinstance(floor, bool) and floor >= 0 else 0


def effective_reserve_tokens(context_window: int | None, floor: int = 0) -> int:
    """15% output reserve over the catalog window; None/degenerate → floor, never raises."""
    safe_floor = _reserve_floor(floor)
    if context_window is None or isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        return safe_floor
    return max(
        safe_floor,
        (context_window * _TOKEN_RESERVE_NUMERATOR) // _TOKEN_RESERVE_DENOMINATOR,
    )


def resolve_budget_reserve_tokens(
    context_window: int | None,
    *,
    reserve_tokens: int | None = None,
    floor: int = DEFAULT_CONTEXT_RESERVE_TOKENS,
) -> int:
    """Budget reserve with upstream's small-window recovery for a defaulted floor.

    Mirrors omp ``resolveBudgetReserveTokens``: a *defaulted* reserve that is
    impossible for the window (``reserve >= cw - 15%*cw``, or ``reserve >= cw``)
    falls back to the 15% proportional reserve so the derived threshold stays
    usable. An explicit ``reserve_tokens`` — even one equal to the default —
    always wins. Provenance is the argument being ``None``, never a value
    comparison.
    """
    if reserve_tokens is not None and isinstance(reserve_tokens, int) and not isinstance(reserve_tokens, bool) and reserve_tokens >= 0:
        return reserve_tokens
    reserve = effective_reserve_tokens(context_window, floor)
    if context_window is None or isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        return reserve
    proportional = max(1, (context_window * _TOKEN_RESERVE_NUMERATOR) // _TOKEN_RESERVE_DENOMINATOR)
    if reserve >= context_window - proportional or reserve >= context_window:
        return proportional
    return reserve


def _clamp_threshold(value: int, context_window: int) -> int:
    upper = max(1, context_window - 1)
    return max(1, min(value, upper))


def _coerce_threshold_int(value: int | None) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def resolve_threshold_tokens(
    context_window: int | None,
    *,
    threshold_tokens: int | None = None,
    reserve_tokens: int | None = None,
    floor: int = DEFAULT_CONTEXT_RESERVE_TOKENS,
) -> int:
    """An explicit fixed threshold (clamped [1, cw-1]) beats the derived one.

    An explicit ``threshold_tokens`` is authoritative even without a catalog
    window: only the reserve-derived threshold needs one, so a model the catalog
    does not describe can still opt into bounded pruning.
    """
    fixed = _coerce_threshold_int(threshold_tokens)
    if context_window is None or isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
        return max(1, fixed) if fixed is not None and fixed > 0 else 0
    if fixed is not None:
        return _clamp_threshold(fixed, context_window)
    reserve = resolve_budget_reserve_tokens(context_window, reserve_tokens=reserve_tokens, floor=floor)
    return max(1, min(context_window - 1, context_window - reserve))


def should_compact(
    context_tokens: int | None,
    context_window: int | None,
    *,
    enabled: bool = True,
    strategy: str = "deterministic",
    threshold_tokens: int | None = None,
    reserve_tokens: int | None = None,
) -> bool:
    """True when usage reaches the compaction threshold; disabled/off/degenerate never compacts."""
    if not enabled:
        return False
    if isinstance(strategy, str) and strategy.lower() in {"off", "disabled"}:
        return False
    tokens = _coerce_threshold_int(context_tokens)
    if tokens is None or tokens < 0:
        return False
    threshold = resolve_threshold_tokens(
        context_window,
        threshold_tokens=threshold_tokens,
        reserve_tokens=reserve_tokens,
    )
    if threshold <= 0:
        return False
    return tokens >= threshold
