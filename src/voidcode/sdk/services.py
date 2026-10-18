"""Scoped runtime services, continuation results, and usage models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .json_values import json_wire_object, own_json_object


@dataclass(frozen=True, slots=True)
class ContinuationResult:
    """Outcome of draining or claiming a durable continuation."""

    continuation_id: str
    owner: str
    session_id: str
    status: Literal["pending", "claimed", "completed", "failed", "cancelled"]
    payload: Mapping[str, object]
    run_status: str | None = None
    run_id: str | None = None
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))

    def as_payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "continuation_id": self.continuation_id,
            "owner": self.owner,
            "session_id": self.session_id,
            "status": self.status,
            "payload": json_wire_object(self.payload),
        }
        if self.run_status is not None:
            result["run_status"] = self.run_status
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.output is not None:
            result["output"] = self.output
        if self.error is not None:
            result["error"] = self.error
        if self.stop_cause is not None:
            result["stop_cause"] = self.stop_cause
        return result


@dataclass(frozen=True, slots=True)
class PluginBudget:
    """Owner-scoped budget constraints for an active goal or task."""

    owner: str
    since_sequence: int = 0
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_elapsed_seconds: float | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "since_sequence": self.since_sequence,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_elapsed_seconds": self.max_elapsed_seconds,
        }


@dataclass(frozen=True, slots=True)
class SessionUsageReport:
    """Truthful aggregate token and cost usage computed strictly from core facts."""

    session_id: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    uncached_input_tokens: int | None
    total_cost_usd: float | None = None
    has_unknown_usage: bool = False
    turn_count: int = 0
    elapsed_seconds: float | None = None
    since_sequence: int = 0
    until_sequence: int | None = None
    through_sequence: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "total_cost_usd": self.total_cost_usd,
            "has_unknown_usage": self.has_unknown_usage,
            "turn_count": self.turn_count,
            "elapsed_seconds": self.elapsed_seconds,
            "since_sequence": self.since_sequence,
            "until_sequence": self.until_sequence,
            "through_sequence": self.through_sequence,
        }


@runtime_checkable
class PluginServices(Protocol):
    """Scoped services available to a plugin handler during lease callback."""

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        """Fetch current state and version for the bound owner in the given scope."""
        ...

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        """Stage optimistic concurrency state replacement for the bound owner."""
        ...

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        """Stage a plugin-namespaced event to be committed atomically with this turn."""
        ...

    def enqueue_continuation(self, payload: Mapping[str, object], *, idempotency_key: str | None = None) -> str:
        """Enqueue a durable continuation to be processed asynchronously by the session."""
        ...

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        """Stage cancellation of pending or claimed continuations owned by this plugin."""
        ...

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Fetch truthful aggregate token usage and elapsed time, windowed by dispatch sequence."""
        ...

    def get_budget(self) -> PluginBudget | None:
        """Fetch active budget limits for the bound owner."""
        ...

    def set_budget(
        self,
        *,
        since_sequence: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        """Stage an owner-scoped budget constraint bound to this session and owner from baseline."""
        ...

    def clear_budget(self) -> None:
        """Stage removal of the bound owner's budget constraint."""
        ...
