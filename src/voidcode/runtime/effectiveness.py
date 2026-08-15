"""Redacted projections for measuring agent-facing tool effectiveness."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import cast

from .events import EventEnvelope


@dataclass(frozen=True, slots=True)
class ToolEffectivenessEvent:
    """One persisted event associated with its durable session."""

    session_id: str
    event: EventEnvelope


@dataclass(slots=True)
class _MutableToolStats:
    calls: int = 0
    successes: int = 0
    errors: int = 0
    retries_after_error: int = 0
    retry_guidance_count: int = 0
    truncated_results: int = 0
    partial_results: int = 0
    argument_bytes: int = 0
    result_bytes: int = 0
    error_kinds: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class _MutableModelEditStats:
    """Mutable per-model edit-tool counters (internal projection state)."""

    calls: int = 0
    successes: int = 0
    errors: int = 0
    ambiguous_match_count: int = 0
    error_kinds: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class ToolEffectivenessStats:
    tool: str
    calls: int
    successes: int
    errors: int
    retries_after_error: int
    retry_guidance_count: int
    truncated_results: int
    partial_results: int
    argument_bytes: int
    result_bytes: int
    error_kinds: Mapping[str, int]

    @property
    def success_rate(self) -> float | None:
        if self.calls == 0:
            return None
        return self.successes / self.calls

    def to_payload(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "successes": self.successes,
            "errors": self.errors,
            "success_rate": self.success_rate,
            "retries_after_error": self.retries_after_error,
            "retry_guidance_count": self.retry_guidance_count,
            "truncated_results": self.truncated_results,
            "partial_results": self.partial_results,
            "argument_bytes": self.argument_bytes,
            "result_bytes": self.result_bytes,
            "average_argument_bytes": self.argument_bytes / self.calls if self.calls else None,
            "average_result_bytes": self.result_bytes / self.calls if self.calls else None,
            "error_kinds": dict(self.error_kinds),
        }


@dataclass(frozen=True, slots=True)
class ModelEditEffectivenessStats:
    """Per-model edit-tool outcomes, consumed by edit-schema policy selection.

    ``model`` is the model reference carried by ``runtime.tool_completed``
    events (additive metadata; events without a model are not attributable and
    are excluded from this breakdown).
    """

    model: str
    edit_calls: int
    edit_successes: int
    edit_errors: int
    edit_ambiguous_match_count: int
    edit_error_kinds: Mapping[str, int]

    @property
    def edit_ambiguous_match_rate(self) -> float | None:
        if self.edit_calls == 0:
            return None
        return self.edit_ambiguous_match_count / self.edit_calls

    def to_payload(self) -> dict[str, object]:
        return {
            "model": self.model,
            "edit_calls": self.edit_calls,
            "edit_successes": self.edit_successes,
            "edit_errors": self.edit_errors,
            "edit_ambiguous_match_count": self.edit_ambiguous_match_count,
            "edit_ambiguous_match_rate": self.edit_ambiguous_match_rate,
            "edit_error_kinds": dict(self.edit_error_kinds),
        }


@dataclass(frozen=True, slots=True)
class ToolEffectivenessReport:
    schema_version: int
    workspace_id: str
    session_count: int
    tool_call_count: int
    success_count: int
    error_count: int
    repeated_read_count: int
    followup_read_count: int
    compaction_count: int
    approval_request_count: int
    resumed_run_count: int
    delegated_task_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    uncached_input_tokens: int
    tools: tuple[ToolEffectivenessStats, ...]
    models: tuple[ModelEditEffectivenessStats, ...] = ()

    @property
    def success_rate(self) -> float | None:
        if self.tool_call_count == 0:
            return None
        return self.success_count / self.tool_call_count

    @property
    def cache_hit_rate(self) -> float | None:
        denominator = self.cache_read_tokens + self.uncached_input_tokens
        if denominator <= 0:
            return None
        return self.cache_read_tokens / denominator

    def edit_stats_for_model(self, model: str) -> ModelEditEffectivenessStats | None:
        for stats in self.models:
            if stats.model == model:
                return stats
        return None

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "session_count": self.session_count,
            "tool_call_count": self.tool_call_count,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "success_rate": self.success_rate,
            "repeated_read_count": self.repeated_read_count,
            "followup_read_count": self.followup_read_count,
            "compaction_count": self.compaction_count,
            "approval_request_count": self.approval_request_count,
            "resumed_run_count": self.resumed_run_count,
            "delegated_task_count": self.delegated_task_count,
            "provider_usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "uncached_input_tokens": self.uncached_input_tokens,
                "cache_hit_rate": self.cache_hit_rate,
            },
            "tools": [tool.to_payload() for tool in self.tools],
            "models": [model.to_payload() for model in self.models],
            "privacy": {
                "source": "persisted_runtime_events",
                "stores_source_content": False,
                "stores_arguments": False,
                "projection": "aggregate_only",
            },
        }


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def _result_size(payload: Mapping[str, object]) -> int:
    content = payload.get("content")
    data_without_arguments = {key: value for key, value in payload.items() if key not in {"arguments", "display", "tool_status", "content"}}
    return _json_size(data_without_arguments) + (len(content.encode("utf-8")) if isinstance(content, str) else 0)


def project_tool_effectiveness(
    *,
    workspace_id: str,
    session_ids: Iterable[str],
    session_metadata: Mapping[str, Mapping[str, object]] | None = None,
    events: Iterable[ToolEffectivenessEvent],
) -> ToolEffectivenessReport:
    """Aggregate persisted tool events without retaining arguments or result text."""

    session_id_set = set(session_ids)
    mutable: dict[str, _MutableToolStats] = {}
    model_edit_mutable: dict[str, _MutableModelEditStats] = {}
    pending_errors: set[tuple[str, str]] = set()
    read_paths_by_session: dict[str, set[str]] = {}
    pending_partial_reads: set[tuple[str, str]] = set()
    repeated_read_count = 0
    followup_read_count = 0
    compaction_count = 0
    approval_request_count = 0
    request_counts: Counter[str] = Counter()

    for item in events:
        event = item.event
        if event.event_type == "runtime.request_received":
            request_counts[item.session_id] += 1
            continue
        if event.event_type == "runtime.context_compacted":
            compaction_count += 1
            continue
        if event.event_type == "runtime.approval_requested":
            approval_request_count += 1
            continue
        if event.event_type != "runtime.tool_completed":
            continue
        payload = event.payload
        raw_tool = payload.get("tool")
        tool = raw_tool if isinstance(raw_tool, str) and raw_tool else "unknown"
        stats = mutable.setdefault(tool, _MutableToolStats())
        stats.calls += 1

        raw_model = payload.get("model")
        model = raw_model if isinstance(raw_model, str) and raw_model else None
        model_stats: _MutableModelEditStats | None = None
        if tool == "edit" and model is not None:
            model_stats = model_edit_mutable.setdefault(model, _MutableModelEditStats())
            model_stats.calls += 1

        arguments = payload.get("arguments")
        if isinstance(arguments, Mapping):
            stats.argument_bytes += _json_size(arguments)
        stats.result_bytes += _result_size(payload)

        if payload.get("truncated") is True:
            stats.truncated_results += 1
        if payload.get("partial") is True:
            stats.partial_results += 1
        if isinstance(payload.get("retry_guidance"), str):
            stats.retry_guidance_count += 1

        retry_key = (item.session_id, tool)
        is_error = payload.get("status") == "error" or payload.get("error") is not None
        if is_error:
            stats.errors += 1
            raw_error_kind = payload.get("error_kind")
            error_kind = raw_error_kind if isinstance(raw_error_kind, str) and raw_error_kind else "unspecified"
            stats.error_kinds[error_kind] += 1
            if model_stats is not None:
                model_stats.errors += 1
                model_stats.error_kinds[error_kind] += 1
                if error_kind == "ambiguous_match":
                    model_stats.ambiguous_match_count += 1
            pending_errors.add(retry_key)
            continue

        stats.successes += 1
        if model_stats is not None:
            model_stats.successes += 1
        if tool == "read_file" and isinstance(arguments, Mapping):
            typed_arguments = cast(Mapping[str, object], arguments)
            raw_path = typed_arguments.get("path")
            if isinstance(raw_path, str) and raw_path:
                seen_paths = read_paths_by_session.setdefault(item.session_id, set())
                read_key = (item.session_id, raw_path)
                if raw_path in seen_paths:
                    repeated_read_count += 1
                if read_key in pending_partial_reads:
                    followup_read_count += 1
                    pending_partial_reads.remove(read_key)
                seen_paths.add(raw_path)
                if payload.get("truncated") is True or payload.get("partial") is True:
                    pending_partial_reads.add(read_key)
        if retry_key in pending_errors:
            stats.retries_after_error += 1
            pending_errors.remove(retry_key)

    tools = tuple(
        ToolEffectivenessStats(
            tool=tool,
            calls=stats.calls,
            successes=stats.successes,
            errors=stats.errors,
            retries_after_error=stats.retries_after_error,
            retry_guidance_count=stats.retry_guidance_count,
            truncated_results=stats.truncated_results,
            partial_results=stats.partial_results,
            argument_bytes=stats.argument_bytes,
            result_bytes=stats.result_bytes,
            error_kinds=dict(sorted(stats.error_kinds.items())),
        )
        for tool, stats in sorted(mutable.items(), key=lambda pair: (-pair[1].calls, pair[0]))
    )
    success_count = sum(tool.successes for tool in tools)
    error_count = sum(tool.errors for tool in tools)
    models = tuple(
        ModelEditEffectivenessStats(
            model=model,
            edit_calls=stats.calls,
            edit_successes=stats.successes,
            edit_errors=stats.errors,
            edit_ambiguous_match_count=stats.ambiguous_match_count,
            edit_error_kinds=dict(sorted(stats.error_kinds.items())),
        )
        for model, stats in sorted(model_edit_mutable.items())
    )
    metadata_by_session = session_metadata or {}
    usage_totals = Counter[str]()
    for metadata in metadata_by_session.values():
        provider_usage = metadata.get("provider_usage")
        if not isinstance(provider_usage, Mapping):
            continue
        typed_provider_usage = cast(Mapping[str, object], provider_usage)
        cumulative = typed_provider_usage.get("cumulative")
        if not isinstance(cumulative, Mapping):
            continue
        typed_cumulative = cast(Mapping[str, object], cumulative)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "uncached_input_tokens"):
            value = typed_cumulative.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage_totals[key] += value
    return ToolEffectivenessReport(
        schema_version=1,
        workspace_id=workspace_id,
        session_count=len(session_id_set),
        tool_call_count=success_count + error_count,
        success_count=success_count,
        error_count=error_count,
        repeated_read_count=repeated_read_count,
        followup_read_count=followup_read_count,
        compaction_count=compaction_count,
        approval_request_count=approval_request_count,
        resumed_run_count=sum(max(count - 1, 0) for count in request_counts.values()),
        delegated_task_count=next((tool.calls for tool in tools if tool.tool == "task"), 0),
        input_tokens=usage_totals["input_tokens"],
        output_tokens=usage_totals["output_tokens"],
        cache_read_tokens=usage_totals["cache_read_tokens"],
        cache_write_tokens=usage_totals["cache_write_tokens"],
        uncached_input_tokens=usage_totals["uncached_input_tokens"],
        tools=tools,
        models=models,
    )
