"""Goal token accounting: Core budget staging, dispatch sums, and window spend."""

from __future__ import annotations

from voidcode.sdk.provider import ProviderTokenUsage
from voidcode.sdk.services import BudgetServices

from .state import ActiveWindow


def _apply_budget(services: BudgetServices, *, since_sequence: int, max_total_tokens: int | None) -> None:
    """Stage the core budget for one lifecycle point; None clears a stale constraint."""
    if max_total_tokens is None:
        services.clear_budget()
    else:
        services.set_budget(since_sequence=since_sequence, max_total_tokens=max_total_tokens)


def _accum_int(total: int | None, reported: int | None) -> int | None:
    """Sum a token metric truthfully: an unknown contributor keeps the total unknown."""
    if total is None or reported is None:
        return None
    return total + reported


def _accum_float(total: float | None, reported: float | None) -> float | None:
    """Sum a measured metric truthfully: an unknown contributor keeps the total unknown."""
    if total is None or reported is None:
        return None
    return total + reported


def _get_closed_window_spend(
    services: BudgetServices,
    windows: tuple[ActiveWindow, ...],
) -> int | None:
    """Closed-window token spend, or ``None`` when any window's usage is unreported."""
    total = 0
    for w in windows:
        if w.end_sequence is None:
            continue
        report = services.get_usage(since_sequence=w.start_sequence, until_sequence=w.end_sequence)
        if report.has_unknown_usage or report.input_tokens is None or report.output_tokens is None:
            return None
        total += report.input_tokens + report.output_tokens
    return total


def _fact_total_tokens(usage: ProviderTokenUsage) -> int | None:
    """Billable token total for one dispatch; unknown when either side was unreported."""
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    return usage.input_tokens + usage.output_tokens
