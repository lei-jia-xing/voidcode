"""Truthful usage accounting over one session's committed provider facts.

A usage report is a projection of the append-only event stream: dispatches inside the
requested window, their matching model-turn or interruption completions, and the
unknown-usage distinctions between "not reported" and "zero". This collaborator reads
only durable state, so the core can rebuild it per turn without owning the arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..sdk.codec import decode_provider_result
from ..sdk.json_values import json_wire_object
from ..sdk.services import SessionUsageReport
from .events import EventType
from .storage.store import SessionRecord, SqliteStore, StoredEvent


def _wire_object(value: object, what: str) -> Mapping[str, object]:
    """Require a JSON object from a durable payload, failing visibly when malformed."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a JSON object")
    return json_wire_object(value)


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
        all_events = self._store.events_by_types(session_id, (EventType.PROVIDER_INTENT, EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED))
        dispatches_in_window: list[int] = []
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
            elif ev.event_type in (EventType.MODEL_TURN, EventType.PROVIDER_INTERRUPTED):
                disp_seq = ev.payload.get("dispatch_sequence")
                if disp_seq is not None and isinstance(disp_seq, (int, float)):
                    completions_by_dispatch[int(disp_seq)] = ev

        if not dispatches_in_window:
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

        for disp_seq in dispatches_in_window:
            comp_ev = completions_by_dispatch.get(disp_seq)
            if comp_ev is None or comp_ev.event_type == EventType.PROVIDER_INTERRUPTED:
                unknown_input = True
                unknown_output = True
                unknown_cache = True
                unknown_uncached = True
                unknown_cost = True
                if comp_ev is not None:
                    ms = comp_ev.payload.get("elapsed_ms")
                    if ms is not None and isinstance(ms, (int, float)):
                        total_elapsed_ms += int(ms)
                    else:
                        unknown_elapsed = True
                else:
                    unknown_elapsed = True
            elif comp_ev.event_type == EventType.MODEL_TURN:
                turn_count += 1
                turn = decode_provider_result(_wire_object(comp_ev.payload["result"], "model turn"))
                u = turn.usage
                if u is not None and u.input_tokens is not None:
                    total_input += u.input_tokens
                else:
                    unknown_input = True

                if u is not None and u.output_tokens is not None:
                    total_output += u.output_tokens
                else:
                    unknown_output = True

                if u is not None and u.cache_read_tokens is not None:
                    total_cache_read += u.cache_read_tokens
                else:
                    unknown_cache = True

                if u is not None and u.uncached_input_tokens is not None:
                    total_uncached_input += u.uncached_input_tokens
                else:
                    unknown_uncached = True

                # One canonical usage DTO carries cost; the event envelope may also
                # carry it, and either source is truthful for this dispatch.
                cost = comp_ev.payload.get("cost_usd")
                if cost is None and u is not None and u.cost_usd is not None:
                    cost = u.cost_usd
                if cost is not None and isinstance(cost, (int, float)):
                    total_cost += float(cost)
                else:
                    unknown_cost = True

                ms = comp_ev.payload.get("elapsed_ms")
                if ms is not None and isinstance(ms, (int, float)):
                    total_elapsed_ms += int(ms)
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
