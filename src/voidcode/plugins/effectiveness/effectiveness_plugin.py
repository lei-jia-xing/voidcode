"""Effectiveness plugin: aggregate-only tool-outcome projection for one session.

Ports the aggregate projection of legacy ``runtime/effectiveness.py`` plus
``runtime/storage/effectiveness.py`` onto core seams: the projection reads this
session's committed events (``services.read_events``, paged) and its truthful usage
report (``services.get_usage``), and reports counts only.

Truthfulness rules kept from the legacy projection:

- arguments, result text, and source content are never retained, only byte sizes;
- an unreadable event (truncated payload, missing tool identity) is counted as
  unknown, never attributed to a guessed tool;
- a usage sum whose input was unknown stays ``None`` and sets ``has_unknown``,
  and an unknown total is never reported as ``0``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.json_values import json_wire_value
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import EventView, PluginServices, SessionUsageReport
from voidcode.sdk.tools import ToolEffect

SCHEMA_VERSION = 1
#: Events per ``read_events`` page; paging stops when a page comes back short.
EVENT_PAGE_SIZE = 200

#: Core facts this projection aggregates.
MODEL_TURN_EVENT = "kernel.model_turn"
#: One tool outcome: completed normally, or interrupted with an unsettled effect.
TOOL_OUTCOME_EVENTS: frozenset[str] = frozenset({"kernel.tool_completed", "kernel.tool_interrupted"})
APPROVAL_REQUESTED_EVENT = "kernel.approval_requested"
#: One run request: an authentic user prompt, or a run re-driven after a crash.
RUN_REQUEST_EVENTS: frozenset[str] = frozenset({"kernel.user_message", "kernel.run_resumed"})
#: Compaction facts, owned by the compaction surface, not by this plugin. The current name is
#: the stable cumulative fact (one per applied projection); the legacy name keeps counting
#: historical sessions whose facts were staged before namespacing.
COMPACTED_EVENTS: frozenset[str] = frozenset({"plugin.compaction.context_compacted", "runtime.context_compacted"})

READ_TOOL = "read"
EDIT_TOOL = "edit"
DELEGATION_TOOL = "task"


@dataclass(frozen=True, slots=True)
class ToolEffectivenessStats:
    """Aggregated outcomes of one tool; never carries arguments or result text."""

    tool: str
    calls: int
    successes: int
    errors: int
    unknown_results: int
    retries_after_error: int
    retry_guidance_count: int
    truncated_results: int
    partial_results: int
    argument_bytes: int
    result_bytes: int
    error_kinds: Mapping[str, int]

    @property
    def success_rate(self) -> float | None:
        return None if self.calls == 0 else self.successes / self.calls

    @property
    def average_argument_bytes(self) -> float | None:
        return None if self.calls == 0 else self.argument_bytes / self.calls

    @property
    def average_result_bytes(self) -> float | None:
        return None if self.calls == 0 else self.result_bytes / self.calls

    def to_payload(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "successes": self.successes,
            "errors": self.errors,
            "unknown_results": self.unknown_results,
            "success_rate": self.success_rate,
            "retries_after_error": self.retries_after_error,
            "retry_guidance_count": self.retry_guidance_count,
            "truncated_results": self.truncated_results,
            "partial_results": self.partial_results,
            "argument_bytes": self.argument_bytes,
            "result_bytes": self.result_bytes,
            "average_argument_bytes": self.average_argument_bytes,
            "average_result_bytes": self.average_result_bytes,
            "error_kinds": dict(self.error_kinds),
        }


@dataclass(frozen=True, slots=True)
class ModelEditEffectivenessStats:
    """Per-model edit-tool outcomes; edits without a model are not attributable."""

    model: str
    edit_calls: int
    edit_successes: int
    edit_errors: int
    edit_ambiguous_match_count: int
    edit_error_kinds: Mapping[str, int]

    @property
    def edit_ambiguous_match_rate(self) -> float | None:
        return None if self.edit_calls == 0 else self.edit_ambiguous_match_count / self.edit_calls

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
class EffectivenessReport:
    """Aggregate-only projection of one session; ``None`` means unknown, never zero."""

    workspace_id: str
    session_id: str
    tool_call_count: int
    success_count: int
    error_count: int
    repeated_read_count: int
    followup_read_count: int
    compaction_count: int
    approval_request_count: int
    resumed_run_count: int
    delegated_task_count: int
    unattributed_tool_events: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    uncached_input_tokens: int | None
    cost_usd: float | None
    has_unknown_usage: bool
    tools: tuple[ToolEffectivenessStats, ...] = ()
    models: tuple[ModelEditEffectivenessStats, ...] = ()

    @property
    def success_rate(self) -> float | None:
        return None if self.tool_call_count == 0 else self.success_count / self.tool_call_count

    @property
    def cache_hit_rate(self) -> float | None:
        if self.cache_read_tokens is None or self.uncached_input_tokens is None:
            return None
        denominator = self.cache_read_tokens + self.uncached_input_tokens
        return None if denominator <= 0 else self.cache_read_tokens / denominator

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            # This projection reads exactly the bound session's own committed events.
            "session_count": 1,
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
            "unattributed_tool_events": self.unattributed_tool_events,
            "provider_usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "uncached_input_tokens": self.uncached_input_tokens,
                "cache_hit_rate": self.cache_hit_rate,
                "cost_usd": self.cost_usd,
                "has_unknown": self.has_unknown_usage,
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


@dataclass(slots=True)
class _MutableToolStats:
    """Mutable per-tool counters (internal projection state)."""

    calls: int = 0
    successes: int = 0
    errors: int = 0
    unknown_results: int = 0
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


def _json_size(value: object) -> int:
    """JSON bytes of an owned event snapshot; containers are projected before measuring."""
    return len(json.dumps(json_wire_value(value), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def _result_size(result: Mapping[str, object]) -> int:
    """Envelope bytes plus rendered output text; arguments are never part of a result."""
    output = result.get("output")
    text = output.get("text") if isinstance(output, Mapping) else None
    envelope = {key: value for key, value in result.items() if key != "output"}
    return _json_size(envelope) + (len(text.encode("utf-8")) if isinstance(text, str) else 0)


def _bounds_flag(result: Mapping[str, object], flag: str) -> bool:
    output = result.get("output")
    bounds = output.get("bounds") if isinstance(output, Mapping) else None
    return isinstance(bounds, Mapping) and bounds.get(flag) is True


def read_session_events(services: PluginServices) -> tuple[EventView, ...]:
    """Read every committed event of this session, one bounded page at a time."""
    events: list[EventView] = []
    after_sequence = 0
    while True:
        page = services.read_events(after_sequence=after_sequence, limit=EVENT_PAGE_SIZE, include_payload=True)
        events.extend(page)
        if len(page) < EVENT_PAGE_SIZE:
            return tuple(events)
        after_sequence = page[-1].sequence


def _tool_outcome(payload: Mapping[str, object]) -> tuple[str, Mapping[str, object] | None, Mapping[str, object]] | None:
    """Tool identity, call arguments (when present), and result of one outcome event."""
    call = payload.get("call")
    result = payload.get("result")
    call_payload = call if isinstance(call, Mapping) else {}
    result_payload = result if isinstance(result, Mapping) else {}
    raw_tool = call_payload.get("tool_name") or result_payload.get("tool_name")
    if not isinstance(raw_tool, str) or not raw_tool:
        return None
    raw_arguments = call_payload.get("arguments")
    return raw_tool, (raw_arguments if isinstance(raw_arguments, Mapping) else None), result_payload


def _edit_model(payload: Mapping[str, object], models_by_batch: Mapping[int, str]) -> str | None:
    """Edit attribution: the payload's own model when present, else its batch's model turn."""
    raw_model = payload.get("model")
    if isinstance(raw_model, str) and raw_model:
        return raw_model
    batch_sequence = payload.get("batch_sequence")
    if isinstance(batch_sequence, int) and not isinstance(batch_sequence, bool):
        return models_by_batch.get(batch_sequence)
    return None


def project_session_effectiveness(
    *,
    workspace_id: str,
    session_id: str,
    events: Iterable[EventView],
    usage: SessionUsageReport,
) -> EffectivenessReport:
    """Aggregate this session's tool outcomes without retaining arguments or result text."""
    stats_by_tool: dict[str, _MutableToolStats] = {}
    stats_by_model: dict[str, _MutableModelEditStats] = {}
    models_by_batch: dict[int, str] = {}
    pending_errors: set[str] = set()
    seen_read_paths: set[str] = set()
    pending_partial_reads: set[str] = set()
    repeated_read_count = 0
    followup_read_count = 0
    compaction_count = 0
    approval_request_count = 0
    run_request_count = 0
    unattributed_tool_events = 0

    for view in events:
        event_type = view.event_type
        payload = view.payload if isinstance(view.payload, Mapping) else {}
        if event_type == MODEL_TURN_EVENT:
            model = payload.get("model")
            if isinstance(model, str) and model:
                models_by_batch[view.sequence] = model
            continue
        if event_type in COMPACTED_EVENTS:
            compaction_count += 1
            continue
        if event_type == APPROVAL_REQUESTED_EVENT:
            # Legacy parity: an approval announced by a hook preset is not a user request.
            if payload.get("hook_status") is None:
                approval_request_count += 1
            continue
        if event_type in RUN_REQUEST_EVENTS:
            run_request_count += 1
            continue
        if event_type not in TOOL_OUTCOME_EVENTS:
            continue

        outcome = _tool_outcome(payload)
        if outcome is None:
            # A truncated or identity-less payload cannot be attributed to any tool.
            unattributed_tool_events += 1
            continue
        tool, arguments, result = outcome
        stats = stats_by_tool.setdefault(tool, _MutableToolStats())
        stats.calls += 1

        model_stats: _MutableModelEditStats | None = None
        if tool == EDIT_TOOL:
            model = _edit_model(payload, models_by_batch)
            if model is not None:
                model_stats = stats_by_model.setdefault(model, _MutableModelEditStats())
                model_stats.calls += 1

        if arguments is not None:
            stats.argument_bytes += _json_size(arguments)
        stats.result_bytes += _result_size(result)

        truncated = _bounds_flag(result, "truncated")
        partial = _bounds_flag(result, "partial")
        if truncated:
            stats.truncated_results += 1
        if partial:
            stats.partial_results += 1
        raw_diagnostics = result.get("diagnostics")
        diagnostics = raw_diagnostics if isinstance(raw_diagnostics, Mapping) else {}
        if isinstance(diagnostics.get("guidance"), str):
            stats.retry_guidance_count += 1

        status = result.get("status")
        if status == "error" or result.get("error") is not None:
            raw_error_kind = diagnostics.get("kind")
            error_kind = raw_error_kind if isinstance(raw_error_kind, str) and raw_error_kind else "unspecified"
            stats.errors += 1
            stats.error_kinds[error_kind] += 1
            if model_stats is not None:
                model_stats.errors += 1
                model_stats.error_kinds[error_kind] += 1
                if error_kind == "ambiguous_match":
                    model_stats.ambiguous_match_count += 1
            pending_errors.add(tool)
            continue
        if status != "ok":
            # A result that states neither outcome is unknown, never counted as either.
            stats.unknown_results += 1
            continue

        stats.successes += 1
        if model_stats is not None:
            model_stats.successes += 1
        if tool == READ_TOOL and arguments is not None:
            raw_path = arguments.get("path")
            if isinstance(raw_path, str) and raw_path:
                if raw_path in seen_read_paths:
                    repeated_read_count += 1
                if raw_path in pending_partial_reads:
                    followup_read_count += 1
                    pending_partial_reads.discard(raw_path)
                seen_read_paths.add(raw_path)
                if truncated or partial:
                    pending_partial_reads.add(raw_path)
        if tool in pending_errors:
            stats.retries_after_error += 1
            pending_errors.discard(tool)

    tools = tuple(
        ToolEffectivenessStats(
            tool=tool,
            calls=stats.calls,
            successes=stats.successes,
            errors=stats.errors,
            unknown_results=stats.unknown_results,
            retries_after_error=stats.retries_after_error,
            retry_guidance_count=stats.retry_guidance_count,
            truncated_results=stats.truncated_results,
            partial_results=stats.partial_results,
            argument_bytes=stats.argument_bytes,
            result_bytes=stats.result_bytes,
            error_kinds=dict(sorted(stats.error_kinds.items())),
        )
        for tool, stats in sorted(stats_by_tool.items(), key=lambda pair: (-pair[1].calls, pair[0]))
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
        for model, stats in sorted(stats_by_model.items())
    )
    usage_values = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.uncached_input_tokens,
        usage.total_cost_usd,
    )
    return EffectivenessReport(
        workspace_id=workspace_id,
        session_id=session_id,
        tool_call_count=success_count + error_count,
        success_count=success_count,
        error_count=error_count,
        repeated_read_count=repeated_read_count,
        followup_read_count=followup_read_count,
        compaction_count=compaction_count,
        approval_request_count=approval_request_count,
        resumed_run_count=max(run_request_count - 1, 0),
        delegated_task_count=next((tool.calls for tool in tools if tool.tool == DELEGATION_TOOL), 0),
        unattributed_tool_events=unattributed_tool_events,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        uncached_input_tokens=usage.uncached_input_tokens,
        cost_usd=usage.total_cost_usd,
        has_unknown_usage=usage.has_unknown_usage or any(value is None for value in usage_values),
        tools=tools,
        models=models,
    )


def _format_rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def render_summary(report: EffectivenessReport) -> str:
    """Short human-readable rendering of the projection (legacy ``stats tools`` lines)."""
    lines = [
        "TOOL EFFECTIVENESS "
        f"calls={report.tool_call_count} success={report.success_count} errors={report.error_count} "
        f"success_rate={_format_rate(report.success_rate)}",
        "SIGNALS "
        f"repeated_reads={report.repeated_read_count} followup_reads={report.followup_read_count} "
        f"compactions={report.compaction_count} approvals={report.approval_request_count} "
        f"resumed_runs={report.resumed_run_count} delegated_tasks={report.delegated_task_count}",
        "TOKENS "
        f"input={report.input_tokens} output={report.output_tokens} "
        f"cache_read={report.cache_read_tokens} uncached_input={report.uncached_input_tokens} "
        f"cache_hit_rate={_format_rate(report.cache_hit_rate)} cost_usd={report.cost_usd} "
        f"unknown={report.has_unknown_usage}",
    ]
    if not report.tools:
        lines.append("No persisted tool calls for this session.")
        return "\n".join(lines)
    lines.append("TOOL                         CALLS     OK  ERRORS   RATE  RETRIES  TRUNCATED  ERROR KINDS")
    for tool in report.tools:
        kinds = ", ".join(f"{kind}:{count}" for kind, count in tool.error_kinds.items()) or "-"
        lines.append(
            f"{tool.tool[:28]:<28} {tool.calls:>5} {tool.successes:>6} {tool.errors:>7} "
            f"{_format_rate(tool.success_rate):>6} {tool.retries_after_error:>8} {tool.truncated_results:>10}  {kinds}"
        )
    return "\n".join(lines)


class EffectivenessCommand:
    """Report this session's aggregate tool-outcome projection."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="effectiveness",
        description="Show aggregate tool-effectiveness metrics for this session.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        context = invocation.context
        report = project_session_effectiveness(
            workspace_id=str(context.require_workspace()),
            session_id=context.require_session_id(),
            events=read_session_events(services),
            usage=services.get_usage(),
        )
        return CommandResult(status="ok", output=render_summary(report), data=report.to_payload())


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(EffectivenessCommand(),))
