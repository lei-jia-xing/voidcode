"""Runtime-owned context projection contracts.

The projection is deliberately provider-agnostic.  Runtime compaction builds
the structured facts; optional summary providers may only replace the textual
projection and must fall back to the deterministic text on failure.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal, Protocol


class SummaryProjector(Protocol):
    def project(self, *, facts: Mapping[str, object], deterministic_summary: str) -> str: ...


def project_summary(
    *,
    strategy: Literal["deterministic", "model_assisted"],
    facts: Mapping[str, object],
    deterministic_summary: str,
    projector: Callable[[Mapping[str, object]], str] | None = None,
) -> tuple[str, Literal["deterministic", "model_assisted", "fallback"], str | None]:
    """Return (summary, actual strategy, fallback reason)."""
    if strategy != "model_assisted":
        return deterministic_summary, "deterministic", None
    if projector is None:
        return deterministic_summary, "fallback", "model_assisted_projector_unavailable"
    try:
        summary = projector(facts).strip()
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return deterministic_summary, "fallback", f"model_assisted_projector_failed:{type(exc).__name__}"
    if not summary:
        return deterministic_summary, "fallback", "model_assisted_projector_empty"
    return summary, "model_assisted", None


__all__ = ["SummaryProjector", "project_summary"]
