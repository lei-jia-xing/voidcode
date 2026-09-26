"""Token estimator seam: deterministic UTF-8-bytes/4 budget helpers."""

from __future__ import annotations

from voidcode.runtime.context.window import (
    effective_reserve_tokens,
    estimate_tokens_for_bytes,
    resolve_budget_reserve_tokens,
    resolve_threshold_tokens,
    should_compact,
)


def test_estimate_tokens_for_bytes_default_ratio() -> None:
    assert estimate_tokens_for_bytes(400) == 100
    assert estimate_tokens_for_bytes(0) == 0
    assert estimate_tokens_for_bytes(-10) == 0
    # Ceiling: never undercount a partial token.
    assert estimate_tokens_for_bytes(7) == 2


def test_estimate_tokens_for_bytes_custom_ratio() -> None:
    assert estimate_tokens_for_bytes(100, bytes_per_token=2) == 50


def test_effective_reserve_tokens_fifteen_percent() -> None:
    assert effective_reserve_tokens(100000) == 15000


def test_effective_reserve_tokens_floor_and_none() -> None:
    assert effective_reserve_tokens(None) == 0
    assert effective_reserve_tokens(None, floor=200) == 200
    # Explicit floor wins when 15% is smaller.
    assert effective_reserve_tokens(100, floor=500) == 500
    # Degenerate catalog values fall back to floor, never raise.
    assert effective_reserve_tokens(0) == 0
    assert effective_reserve_tokens(-5) == 0


def test_resolve_budget_reserve_tokens() -> None:
    # Upstream floor: ``max(15% of window, 16384)``.
    assert resolve_budget_reserve_tokens(100000) == 16384
    assert resolve_budget_reserve_tokens(1_000_000) == 150000
    assert resolve_budget_reserve_tokens(32_000) == 16384
    assert resolve_budget_reserve_tokens(None) == 16384
    assert resolve_budget_reserve_tokens(None, floor=0) == 0
    # Explicit override wins over the catalog-derived value.
    assert resolve_budget_reserve_tokens(100000, reserve_tokens=42) == 42
    # Small-window recovery (omp ``resolveBudgetReserveTokens``): a defaulted
    # 16384 that leaves no usable budget falls back to the 15% reserve, while
    # an explicit 16384 is honored as configured.
    assert resolve_budget_reserve_tokens(16000) == 2400
    assert resolve_budget_reserve_tokens(16000, reserve_tokens=16384) == 16384


def test_should_compact_default_boundary() -> None:
    # Default threshold = cw - max(15% of cw, 16384) = 83616 for cw=100000; the
    # trigger is strictly greater (omp ``compaction.ts:338``).
    assert should_compact(95000, 100000) is True
    assert should_compact(83615, 100000) is False
    assert should_compact(83616, 100000) is False
    assert should_compact(83617, 100000) is True


def test_should_compact_boundary_is_strictly_greater() -> None:
    # omp ``compaction.ts:338`` compares ``contextTokens > threshold``: usage
    # exactly at the threshold does not compact.
    threshold = resolve_threshold_tokens(100000)
    assert should_compact(threshold, 100000) is False
    assert should_compact(threshold + 1, 100000) is True


def test_should_compact_disabled() -> None:
    assert should_compact(95000, 100000, enabled=False) is False
    assert should_compact(95000, 0) is False
    assert should_compact(95000, -100) is False
    assert should_compact(95000, None) is False


def test_resolve_threshold_tokens_priority_and_clamps() -> None:
    # An explicit fixed threshold wins over the derived one.
    assert resolve_threshold_tokens(100000, threshold_tokens=90000) == 90000
    # Fixed-token clamped to [1, cw-1].
    assert resolve_threshold_tokens(100000, threshold_tokens=0) == 1
    assert resolve_threshold_tokens(100000, threshold_tokens=200000) == 99999
    # Fallback: cw - reserve (max(15%, 16384)); explicit reserve wins.
    assert resolve_threshold_tokens(100000) == 83616
    assert resolve_threshold_tokens(100000, reserve_tokens=42) == 99958
    # 16k window recovers to 13600 instead of collapsing to 1; 32k keeps cw-16384.
    assert resolve_threshold_tokens(16000) == 13600
    assert resolve_threshold_tokens(32000) == 15616
    # Derived threshold never reaches the whole window even when reserve is 0.
    assert resolve_threshold_tokens(16000, reserve_tokens=0) == 15999
