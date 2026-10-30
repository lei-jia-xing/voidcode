"""Aggregate session effectiveness from public, durable SDK facts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.hooks import ApprovalRequestedFact, ChildSpawnedFact, ContextProjectedFact, InvocationFinishedFact, PluginFact, RunRequestedFact
from voidcode.sdk.services import PluginServices, SessionUsageReport
from voidcode.sdk.tools import ToolEffect

SCHEMA_VERSION = 2
FACT_PAGE_SIZE = 200


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
                "uncached_input_tokens": self.uncached_input_tokens,
                "cache_hit_rate": self.cache_hit_rate,
                "cost_usd": self.cost_usd,
                "has_unknown": self.has_unknown_usage,
            },
            "tools": [tool.to_payload() for tool in self.tools],
            "models": [model.to_payload() for model in self.models],
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


def read_session_facts(services: PluginServices) -> Iterable[PluginFact]:
    """Advance through source events, including pages containing no public facts."""
    cursor = 0
    while True:
        page = services.read_facts(after_sequence=cursor, limit=FACT_PAGE_SIZE)
        yield from page.facts
        if not page.has_more:
            return
        cursor = page.next_sequence


def project_session_effectiveness(
    *,
    workspace_id: str,
    session_id: str,
    facts: Iterable[PluginFact],
    usage: SessionUsageReport,
) -> EffectivenessReport:
    """Aggregate this session's tool outcomes without retaining arguments or result text."""
    stats_by_tool: dict[str, _MutableToolStats] = {}
    stats_by_model: dict[str, _MutableModelEditStats] = {}
    pending_errors: set[str] = set()
    seen_reads: set[tuple[str, str, tuple[int, ...], bool, bool]] = set()
    partial_reads: dict[tuple[str, str], set[int]] = {}
    repeated_read_count = followup_read_count = compaction_count = 0
    approval_request_count = resumed_run_count = delegated_task_count = 0

    for fact in facts:
        if isinstance(fact, ContextProjectedFact):
            compaction_count += 1
        elif isinstance(fact, ApprovalRequestedFact):
            approval_request_count += 1
        elif isinstance(fact, RunRequestedFact):
            resumed_run_count += int(fact.resumed)
        elif isinstance(fact, ChildSpawnedFact):
            delegated_task_count += 1
        if not isinstance(fact, InvocationFinishedFact):
            continue
        tool = fact.tool_name
        stats = stats_by_tool.setdefault(tool, _MutableToolStats())
        stats.calls += 1
        stats.argument_bytes += fact.argument_bytes
        stats.result_bytes += fact.result_bytes
        stats.truncated_results += int(fact.truncated is True)
        stats.partial_results += int(fact.partial is True)
        stats.retry_guidance_count += int(fact.has_retry_guidance)
        model_stats = None
        if (ToolEffect.WRITE in fact.effects or fact.changes) and fact.model is not None:
            model_stats = stats_by_model.setdefault(fact.model, _MutableModelEditStats())
            model_stats.calls += 1
        if fact.status == "error":
            kind = fact.error_kind or "unspecified"
            stats.errors += 1
            stats.error_kinds[kind] += 1
            pending_errors.add(tool)
            if model_stats is not None:
                model_stats.errors += 1
                model_stats.error_kinds[kind] += 1
                model_stats.ambiguous_match_count += int(kind == "ambiguous_match")
            continue
        if fact.status != "ok":
            stats.unknown_results += 1
            continue
        stats.successes += 1
        if model_stats is not None:
            model_stats.successes += 1
        if tool in pending_errors:
            stats.retries_after_error += 1
            pending_errors.discard(tool)
        for read in fact.reads:
            identity = (read.path, read.content_hash, read.lines, read.whole_file, read.clipped)
            version = (read.path, read.content_hash)
            repeated_read_count += int(identity in seen_reads)
            seen_reads.add(identity)
            previous = partial_reads.get(version)
            new_lines = set(read.lines)
            if previous is not None and (new_lines - previous or (read.whole_file and not read.clipped)):
                followup_read_count += 1
            if read.whole_file and not read.clipped:
                partial_reads.pop(version, None)
            else:
                partial_reads.setdefault(version, set()).update(new_lines)

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
        tool_call_count=sum(tool.calls for tool in tools),
        success_count=success_count,
        error_count=error_count,
        repeated_read_count=repeated_read_count,
        followup_read_count=followup_read_count,
        compaction_count=compaction_count,
        approval_request_count=approval_request_count,
        resumed_run_count=resumed_run_count,
        delegated_task_count=delegated_task_count,
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
    """Short human-readable rendering of the projection."""
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
            f"{_format_rate(tool.success_rate):>6} {tool.retries_after_error:>8} {str(tool.truncated_results):>10}  {kinds}"
        )
    return "\n".join(lines)


class EffectivenessCommand:
    """Report this session's aggregate tool-outcome projection."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="effectiveness",
        description="Show aggregate tool-effectiveness metrics for this session.",
        argument_schema={"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        context = invocation.context
        report = project_session_effectiveness(
            workspace_id=str(context.require_workspace()),
            session_id=context.require_session_id(),
            facts=read_session_facts(services),
            usage=services.get_usage(),
        )
        offset = invocation.arguments.get("offset", 0)
        count = max(len(report.tools), len(report.models))
        if type(offset) is not int or not 0 <= offset <= count:
            return CommandResult(status="error", error="Metrics changed; reopen /effectiveness from the first page.")

        def measured(value: int | float | None) -> str:
            return "Unknown" if value is None else str(value)

        lines = [
            f"# Session effectiveness\n\nTool calls: {report.tool_call_count} · Successes: {report.success_count} · Errors: {report.error_count}",
            f"Success rate: {'Unknown (no calls)' if report.success_rate is None else _format_rate(report.success_rate)}",
            f"Input tokens: {measured(report.input_tokens)} · Output tokens: {measured(report.output_tokens)}",
            f"Cache-read tokens: {measured(report.cache_read_tokens)} · Uncached input: {measured(report.uncached_input_tokens)}",
            f"Cache hit rate: {'Unknown' if report.cache_hit_rate is None else _format_rate(report.cache_hit_rate)}"
            f" · Cost (USD): {measured(report.cost_usd)}",
            "Usage coverage: incomplete or unknown" if report.has_unknown_usage else "Usage coverage: reported",
            f"Repeated reads: {report.repeated_read_count} · Follow-up reads: {report.followup_read_count}",
            f"Compactions: {report.compaction_count} · Approvals: {report.approval_request_count} · Delegated tasks: {report.delegated_task_count}",
        ]
        if not report.tools:
            lines.append("No persisted tool calls for this session.")
        for tool in report.tools[offset : offset + 10]:
            lines.append(
                f"## {tool.tool[:100]}\nCalls: {tool.calls} · Successes: {tool.successes} · Errors: {tool.errors} "
                f"· Unknown results: {tool.unknown_results}\n"
                f"Retries after error: {tool.retries_after_error} · Truncated: {tool.truncated_results} · Partial: {tool.partial_results}"
            )
            for kind, occurrences in list(tool.error_kinds.items())[:3]:
                lines.append(f"- {kind[:40]}: {occurrences}")
            if len(tool.error_kinds) > 3:
                lines.append(f"Showing 3 of {len(tool.error_kinds)} error categories; full categories remain in structured command data.")
        for model in report.models[offset : offset + 10]:
            lines.append(
                f"Model {model.model[:100]}: {model.edit_calls} edits · {model.edit_errors} errors "
                f"· {model.edit_ambiguous_match_count} ambiguous matches"
            )
        actions: list[dict[str, object]] = [{"label": "Refresh metrics", "action": "effectiveness"}]
        if offset:
            actions.append({"label": "Previous metrics", "action": "effectiveness", "arguments": {"offset": max(0, offset - 10)}})
        if offset + 10 < count:
            actions.append({"label": "Next metrics", "action": "effectiveness", "arguments": {"offset": offset + 10}})
        return CommandResult(
            status="ok",
            output=render_summary(report),
            data=report.to_payload(),
            presentation=CommandPresentation(
                title="Session effectiveness", view_type="markdown", data={"content": "\n\n".join(lines), "actions": actions}
            ),
        )
