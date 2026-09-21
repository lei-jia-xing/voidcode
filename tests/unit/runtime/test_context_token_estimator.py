"""Token estimator seam: deterministic char-based budget helpers (RED phase)."""

from __future__ import annotations

from voidcode.runtime.context.window import (
    TokenBudgetCheck,
    check_token_budget,
    effective_reserve_tokens,
    estimate_tokens_for_chars,
    resolve_budget_reserve_tokens,
    resolve_threshold_tokens,
    should_compact,
)


def test_estimate_tokens_for_chars_default_ratio() -> None:
    assert estimate_tokens_for_chars(400) == 100
    assert estimate_tokens_for_chars(0) == 0
    assert estimate_tokens_for_chars(-10) == 0
    # Ceiling: never undercount a partial token.
    assert estimate_tokens_for_chars(7) == 2


def test_estimate_tokens_for_chars_custom_ratio() -> None:
    assert estimate_tokens_for_chars(100, chars_per_token=2) == 50


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
    assert resolve_budget_reserve_tokens(100000) == 15000
    assert resolve_budget_reserve_tokens(None) == 0
    assert resolve_budget_reserve_tokens(None, floor=200) == 200
    # Explicit override wins over the catalog-derived value.
    assert resolve_budget_reserve_tokens(100000, reserve_tokens=42) == 42


def test_check_token_budget_cheap_first_probe() -> None:
    short = check_token_budget("a" * 10, 1000)
    assert isinstance(short, TokenBudgetCheck)
    assert short.fits is True
    assert short.exact is False

    over = check_token_budget("a" * 10000, 1000)
    assert over.fits is False
    assert over.tokens == estimate_tokens_for_chars(10000)
    assert over.exact is False

    assert check_token_budget("", 0).fits is True
    assert check_token_budget(None, 100).fits is True


def test_should_compact_default_boundary() -> None:
    # Default threshold = cw - 15% reserve = 85000 for cw=100000.
    assert should_compact(95000, 100000) is True
    assert should_compact(84000, 100000) is False
    assert should_compact(85000, 100000) is True


def test_should_compact_disabled() -> None:
    assert should_compact(95000, 100000, enabled=False) is False
    assert should_compact(95000, 100000, strategy="off") is False
    assert should_compact(95000, 100000, strategy="disabled") is False
    assert should_compact(95000, 0) is False
    assert should_compact(95000, -100) is False
    assert should_compact(95000, None) is False


def test_resolve_threshold_tokens_priority_and_clamps() -> None:
    # Fixed-token priority wins over percent.
    assert resolve_threshold_tokens(100000, threshold_tokens=90000, threshold_percent=50) == 90000
    # Fixed-token clamped to [1, cw-1].
    assert resolve_threshold_tokens(100000, threshold_tokens=0) == 1
    assert resolve_threshold_tokens(100000, threshold_tokens=200000) == 99999
    # Percent clamped to [1, 99].
    assert resolve_threshold_tokens(100000, threshold_percent=0) == 1000
    assert resolve_threshold_tokens(100000, threshold_percent=200) == 99000
    assert resolve_threshold_tokens(100000, threshold_percent=50) == 50000
    # Fallback: cw - reserve; explicit reserve wins over derived 15%.
    assert resolve_threshold_tokens(100000) == 85000
    assert resolve_threshold_tokens(100000, reserve_tokens=42) == 99958
