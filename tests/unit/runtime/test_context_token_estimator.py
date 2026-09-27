"""Token counting seam: the model's real tokenizer, with a byte-count fallback."""

from __future__ import annotations

import pytest

from voidcode.provider.tokenizer import count_tokens, known_tokenizers
from voidcode.runtime.context.window import (
    count_payload_bytes,
    effective_reserve_tokens,
    resolve_budget_reserve_tokens,
    resolve_threshold_tokens,
    should_compact,
)


def test_count_payload_bytes_default_ratio() -> None:
    assert count_payload_bytes(400) == 100
    assert count_payload_bytes(0) == 0
    assert count_payload_bytes(-10) == 0
    # Ceiling: never undercount a partial token.
    assert count_payload_bytes(7) == 2


def test_count_payload_tokens_without_tokenizer_uses_the_byte_estimate() -> None:
    """A model with no known tokenizer keeps omp's ``(bytes + 3) >> 2``."""
    assert count_tokens("") == 0
    assert count_tokens("hello world", tokenizer=None) == 3
    # An unshippable name (the Claude family) falls back rather than raising.
    assert count_tokens("hello world", tokenizer="claude-v3") == 3


def test_count_payload_tokens_known_encoding_is_not_the_byte_estimate() -> None:
    """A known encoding must produce a real count, never silently degrade.

    CJK is the discriminating input: four characters are 12 UTF-8 bytes, so the
    byte estimate says 3 while the vocabulary says 5 (verified against oh-my-pi's
    native addon). If a vocabulary ever fails to load and the seam falls back,
    this fails instead of quietly changing every budget number.
    """
    text = "\u4f60\u597d\u4e16\u754c"
    assert count_tokens(text, tokenizer="Cl100kBase") == 5
    assert count_tokens(text, tokenizer=None) == 3


@pytest.mark.parametrize("name", known_tokenizers())
def test_every_shipped_encoding_loads_and_counts(name: str) -> None:
    """Each shipped vocabulary must decode and count; a bad blob fails loudly."""
    assert count_tokens("def foo(x): return x + 1", name) > 0
    assert count_tokens("", name) == 0


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
