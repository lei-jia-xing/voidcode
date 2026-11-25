"""Budget exhaustion, owner budgets, and usage reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from ..json_values import json_wire_object

type BudgetExhaustionDimension = Literal[
    "provider_turns",
    "root_actions",
    "owner_tokens",
    "owner_cost_usd",
    "owner_elapsed_seconds",
]
type BudgetExhaustionSource = Literal[
    "max_steps",
    "root_action_budget",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_cost_usd",
    "max_elapsed_seconds",
]
type BudgetExhaustionContinuation = Literal["resume_with_max_steps", "extend_root_actions", "raise_owner_budget"]

_ALLOWED_BUDGET_EXHAUSTION_FORMS: frozenset[tuple[BudgetExhaustionDimension, BudgetExhaustionSource, BudgetExhaustionContinuation]] = frozenset(
    {
        ("provider_turns", "max_steps", "resume_with_max_steps"),
        ("root_actions", "root_action_budget", "extend_root_actions"),
        ("owner_tokens", "max_input_tokens", "raise_owner_budget"),
        ("owner_tokens", "max_output_tokens", "raise_owner_budget"),
        ("owner_tokens", "max_total_tokens", "raise_owner_budget"),
        ("owner_cost_usd", "max_cost_usd", "raise_owner_budget"),
        ("owner_elapsed_seconds", "max_elapsed_seconds", "raise_owner_budget"),
    }
)


@dataclass(frozen=True, slots=True)
class BudgetExhaustion:
    """One finite execution cap that stopped a run without claiming success.

    Provider-turn and root-action caps keep their existing dimensions. An owner
    budget stop uses ``owner_tokens`` (with the exact ``max_input_tokens``,
    ``max_output_tokens``, or ``max_total_tokens`` source), ``owner_cost_usd``,
    or ``owner_elapsed_seconds``. Its counters are the measured value and the
    configured limit, with ``remaining`` clamped at zero; an unknown metric fails
    before a stop and never produces a guessed exhaustion.
    """

    dimension: BudgetExhaustionDimension
    source: BudgetExhaustionSource
    limit: int | float
    consumed: int | float
    remaining: int | float
    continue_with: BudgetExhaustionContinuation

    def as_payload(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "source": self.source,
            "limit": self.limit,
            "consumed": self.consumed,
            "remaining": self.remaining,
            "continue_with": self.continue_with,
        }

    @classmethod
    def from_payload(cls, value: object) -> BudgetExhaustion | None:
        """Parse a committed typed cap fact; absent stays absent."""
        if value is None:
            return None
        if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
            raise ValueError("budget exhaustion must be an object with string keys or null")
        wire_map: dict[str, object] = {k: v for k, v in value.items() if isinstance(k, str)}
        try:
            parsed = _BUDGET_EXHAUSTION_ADAPTER.validate_json(json.dumps(json_wire_object(wire_map), allow_nan=False), strict=True, extra="forbid")
        except ValidationError as exc:
            raise ValueError(f"budget exhaustion is invalid: {exc}") from exc
        if parsed.limit < 0 or parsed.consumed < 0 or parsed.remaining < 0:
            raise ValueError("budget exhaustion counters must be non-negative numbers")
        if parsed.dimension in ("provider_turns", "root_actions", "owner_tokens") and any(
            type(value) is not int for value in (parsed.limit, parsed.consumed, parsed.remaining)
        ):
            raise ValueError("budget exhaustion counters must be integers for token and turn caps")
        if (parsed.dimension, parsed.source, parsed.continue_with) not in _ALLOWED_BUDGET_EXHAUSTION_FORMS:
            raise ValueError("budget exhaustion has an unknown dimension, source, or continuation")
        return parsed


_BUDGET_EXHAUSTION_ADAPTER: TypeAdapter[BudgetExhaustion] = TypeAdapter(BudgetExhaustion)


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

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> SessionUsageReport:
        if not isinstance(payload, Mapping):
            raise ValueError("usage report payload must be an object")
        return _USAGE_ADAPTER.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


_USAGE_ADAPTER: TypeAdapter[SessionUsageReport] = TypeAdapter(SessionUsageReport)
