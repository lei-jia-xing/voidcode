"""Truthful usage accounting over one session's committed provider facts.

A usage report is a projection of the append-only event stream: dispatches inside the
requested window, their matching model-turn or interruption completions, and the
unknown-usage distinctions between "not reported" and "zero". This collaborator reads
only durable state, so the core can rebuild it per turn without owning the arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ..sdk.codec import decode_provider_result
from ..sdk.provider import ProviderTokenUsage
from ..sdk.services import BudgetExhaustion, PluginBudget, SessionUsageReport
from .events import EventType
from .execution import wire_object
from .storage.store import SessionRecord, SqliteStore, StoredEvent


@dataclass(frozen=True, slots=True)
class BudgetBlock:
    """One active owner budget that refuses the next provider dispatch."""

    #: The cap's own metric is unknown, so this is a failure rather than a reached limit.
    unknown: bool
    message: str
    #: The typed reached cap; never present for an unknown metric.
    exhaustion: BudgetExhaustion | None = None


def _owner_budget_exhaustion(
    *,
    dimension: Literal["owner_tokens", "owner_cost_usd", "owner_elapsed_seconds"],
    source: Literal["max_input_tokens", "max_output_tokens", "max_total_tokens", "max_cost_usd", "max_elapsed_seconds"],
    limit: int | float,
    consumed: int | float,
) -> BudgetExhaustion:
    """Name the exact owner cap and measured spend that refused dispatch."""
    return BudgetExhaustion(
        dimension=dimension,
        source=source,
        limit=limit,
        consumed=consumed,
        remaining=max(0, limit - consumed),
        continue_with="raise_owner_budget",
    )


def budget_block(report: SessionUsageReport, budget: PluginBudget) -> BudgetBlock | None:
    """Return the first metric in ``budget`` that refuses another provider dispatch.

    Each metric is checked independently with its own unknown flag: a known input
    total with an unknown output total must still obey an input cap, and only the
    caps whose metric is genuinely unknown fail visibly.
    """
    if budget.max_input_tokens is not None:
        if report.input_tokens is None:
            return BudgetBlock(True, f"budget limit reached: input tokens unknown for owner {budget.owner}")
        if report.input_tokens >= budget.max_input_tokens:
            return BudgetBlock(
                False,
                f"budget limit exceeded: input tokens {report.input_tokens} >= {budget.max_input_tokens} for owner {budget.owner}",
                _owner_budget_exhaustion(
                    dimension="owner_tokens",
                    source="max_input_tokens",
                    limit=budget.max_input_tokens,
                    consumed=report.input_tokens,
                ),
            )
    if budget.max_output_tokens is not None:
        if report.output_tokens is None:
            return BudgetBlock(True, f"budget limit reached: output tokens unknown for owner {budget.owner}")
        if report.output_tokens >= budget.max_output_tokens:
            return BudgetBlock(
                False,
                f"budget limit exceeded: output tokens {report.output_tokens} >= {budget.max_output_tokens} for owner {budget.owner}",
                _owner_budget_exhaustion(
                    dimension="owner_tokens",
                    source="max_output_tokens",
                    limit=budget.max_output_tokens,
                    consumed=report.output_tokens,
                ),
            )
    if budget.max_total_tokens is not None:
        if report.input_tokens is None or report.output_tokens is None:
            return BudgetBlock(True, f"budget limit reached: total tokens unknown for owner {budget.owner}")
        total = report.input_tokens + report.output_tokens
        if total >= budget.max_total_tokens:
            return BudgetBlock(
                False,
                f"budget limit exceeded: total tokens {total} >= {budget.max_total_tokens} for owner {budget.owner}",
                _owner_budget_exhaustion(
                    dimension="owner_tokens",
                    source="max_total_tokens",
                    limit=budget.max_total_tokens,
                    consumed=total,
                ),
            )
    if budget.max_elapsed_seconds is not None:
        if report.elapsed_seconds is None:
            return BudgetBlock(True, f"budget limit reached: elapsed time unknown for owner {budget.owner}")
        if report.elapsed_seconds >= budget.max_elapsed_seconds:
            return BudgetBlock(
                False,
                f"budget limit exceeded: elapsed time {report.elapsed_seconds}s >= {budget.max_elapsed_seconds}s for owner {budget.owner}",
                _owner_budget_exhaustion(
                    dimension="owner_elapsed_seconds",
                    source="max_elapsed_seconds",
                    limit=budget.max_elapsed_seconds,
                    consumed=report.elapsed_seconds,
                ),
            )
    if budget.max_cost_usd is not None:
        if report.total_cost_usd is None:
            return BudgetBlock(True, f"budget limit reached: cost unknown for owner {budget.owner}")
        if report.total_cost_usd >= budget.max_cost_usd:
            return BudgetBlock(
                False,
                f"budget limit exceeded: cost ${report.total_cost_usd} >= ${budget.max_cost_usd} for owner {budget.owner}",
                _owner_budget_exhaustion(
                    dimension="owner_cost_usd",
                    source="max_cost_usd",
                    limit=budget.max_cost_usd,
                    consumed=report.total_cost_usd,
                ),
            )
    return None


@dataclass(frozen=True, slots=True)
class _DispatchUsage:
    """The bill of one completed provider dispatch, or of an unsettled one."""

    usage: ProviderTokenUsage | None
    cost_usd: float | None
    elapsed_ms: int | float | None
    counts_as_turn: bool


class UsageProjector:
    """Compute one session's usage report from its committed provider events."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    def report(
        self,
        session: SessionRecord,
        *,
        since_sequence: int = 0,
        until_sequence: int | None = None,
    ) -> SessionUsageReport:
        """Calculate truthful token usage metrics strictly from immutable core facts."""
        session_id = session.session_id
        all_events = self._store.events_by_types(
            session_id,
            (EventType.PROVIDER_INTENT, EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED, EventType.MODEL_QUERY),
        )
        dispatches_in_window: list[int] = []
        queries_in_window: list[StoredEvent] = []
        completions_by_dispatch: dict[int, StoredEvent] = {}

        for ev in all_events:
            if ev.sequence > session.last_event_sequence:
                # Only events already inside the captured session tip are reported,
                # so through_sequence never claims less than what was included.
                break
            if ev.event_type == EventType.PROVIDER_INTENT:
                seq = ev.sequence
                if seq > since_sequence and (until_sequence is None or seq <= until_sequence):
                    dispatches_in_window.append(seq)
            elif ev.event_type == EventType.MODEL_QUERY:
                seq = ev.sequence
                if seq > since_sequence and (until_sequence is None or seq <= until_sequence):
                    queries_in_window.append(ev)
            elif ev.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
                disp_seq = ev.payload.get("dispatch_sequence")
                if disp_seq is not None and isinstance(disp_seq, (int, float)):
                    completions_by_dispatch[int(disp_seq)] = ev

        dispatches = [self._dispatch_usage(completions_by_dispatch.get(seq)) for seq in dispatches_in_window]
        dispatches.extend(self._model_query_usage(ev) for ev in queries_in_window)
        if not dispatches:
            return SessionUsageReport(
                session_id=session_id,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                uncached_input_tokens=0,
                total_cost_usd=0.0,
                has_unknown_usage=False,
                turn_count=0,
                elapsed_seconds=0.0,
                since_sequence=since_sequence,
                until_sequence=until_sequence,
                through_sequence=session.last_event_sequence,
            )

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_uncached_input = 0
        total_cost = 0.0
        total_elapsed_ms = 0
        unknown_input = False
        unknown_output = False
        unknown_cache = False
        unknown_uncached = False
        unknown_cost = False
        unknown_elapsed = False
        turn_count = 0

        for dispatch in dispatches:
            if dispatch.counts_as_turn:
                turn_count += 1
            usage = dispatch.usage
            if usage is None:
                unknown_input = True
                unknown_output = True
                unknown_cache = True
                unknown_uncached = True
                unknown_cost = True
                unknown_elapsed = unknown_elapsed or dispatch.elapsed_ms is None
                if dispatch.elapsed_ms is not None:
                    total_elapsed_ms += int(dispatch.elapsed_ms)
                continue

            if usage.input_tokens is not None:
                total_input += usage.input_tokens
            else:
                unknown_input = True

            if usage.output_tokens is not None:
                total_output += usage.output_tokens
            else:
                unknown_output = True

            if usage.cache_read_tokens is not None:
                total_cache_read += usage.cache_read_tokens
            else:
                unknown_cache = True

            if usage.uncached_input_tokens is not None:
                total_uncached_input += usage.uncached_input_tokens
            else:
                unknown_uncached = True

            # One canonical usage DTO carries cost; the event envelope may also
            # carry it, and either source is truthful for this dispatch.
            if dispatch.cost_usd is not None:
                total_cost += dispatch.cost_usd
            else:
                unknown_cost = True

            if dispatch.elapsed_ms is not None:
                total_elapsed_ms += int(dispatch.elapsed_ms)
            else:
                unknown_elapsed = True

        return SessionUsageReport(
            session_id=session_id,
            input_tokens=None if unknown_input else total_input,
            output_tokens=None if unknown_output else total_output,
            cache_read_tokens=None if unknown_cache else total_cache_read,
            uncached_input_tokens=None if unknown_uncached else total_uncached_input,
            total_cost_usd=None if unknown_cost else total_cost,
            has_unknown_usage=unknown_input or unknown_output,
            turn_count=turn_count,
            elapsed_seconds=None if unknown_elapsed else (total_elapsed_ms / 1000.0),
            since_sequence=since_sequence,
            until_sequence=until_sequence,
            through_sequence=session.last_event_sequence,
        )

    @staticmethod
    def _dispatch_usage(completion: StoredEvent | None) -> _DispatchUsage:
        """The bill of one intent's completion, or an unknown bill when it never settled."""
        if completion is None or completion.event_type == EventType.PROVIDER_INTERRUPTED:
            return _DispatchUsage(None, None, _elapsed_ms(completion), False)
        turn = decode_provider_result(wire_object(completion.payload["result"], "model turn"))
        usage = turn.usage
        cost = _cost_usd(completion.payload.get("cost_usd"))
        if cost is None and usage is not None:
            cost = usage.cost_usd
        return _DispatchUsage(usage, cost, _elapsed_ms(completion), True)

    @staticmethod
    def _model_query_usage(event: StoredEvent) -> _DispatchUsage:
        """The bill of one standalone model query, decoded from its own event."""
        usage = _model_query_tokens(event.payload.get("usage"))
        cost = _cost_usd(event.payload.get("cost_usd"))
        if cost is None and usage is not None:
            cost = usage.cost_usd
        return _DispatchUsage(usage, cost, _elapsed_ms(event), True)


def _elapsed_ms(event: StoredEvent | None) -> int | float | None:
    if event is None:
        return None
    value = event.payload.get("elapsed_ms")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cost_usd(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _model_query_tokens(value: object) -> ProviderTokenUsage | None:
    """Decode one model query's recorded usage, keeping absent metrics unknown."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("model query usage must be an object or null")
    tokens: dict[str, int | None] = {}
    for field_name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "uncached_input_tokens"):
        field_value = value.get(field_name)
        if field_value is not None and (type(field_value) is not int or field_value < 0):
            raise ValueError(f"model query usage {field_name} must be a non-negative integer or null")
        tokens[field_name] = field_value
    return ProviderTokenUsage(**tokens, cost_usd=_cost_usd(value.get("cost_usd")))
