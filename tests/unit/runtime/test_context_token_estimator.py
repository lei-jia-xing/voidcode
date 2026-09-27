"""Token counting seam: the model's real tokenizer, with a byte-count fallback."""

from __future__ import annotations

import pytest

from voidcode.provider.tokenizer import count_tokens, known_tokenizers
from voidcode.runtime.context.window import (
    CompactionBudget,
    assemble_provider_context,
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
    # A name outside the catalog falls back rather than raising.
    assert count_tokens("hello world", tokenizer="no-such-encoding") == 3


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


def test_qwen3_combining_marks_do_not_merge_into_a_punctuation_run() -> None:
    """Qwen3's mark handling, pinned on the minimized failure the oracle caught.

    The punctuation-run alternative must exclude ``\\p{M}`` the way upstream's
    `` ?[^\\s\\p{L}\\p{M}\\p{N}]+`` does. When it did not, a variation selector
    (U+FE0F) or combining mark after punctuation was swallowed into the
    punctuation piece instead of standing alone, so a ZWJ + heart + VS16 cluster
    counted 3 where oh-my-pi's native addon says 4. Every value below is the
    oracle's, taken from the native addon — not recomputed from our own splitter.
    """
    assert count_tokens("\u200d\u2764\ufe0f", tokenizer="qwen3") == 4
    assert count_tokens("\u200d\u2764", tokenizer="qwen3") == 3
    assert count_tokens("\u2764\ufe0f", tokenizer="qwen3") == 1
    # A mark exiled from a punctuation run, then the punctuation resumes.
    assert count_tokens(".\u200d\u0301.a", tokenizer="qwen3") == 5
    assert count_tokens("\U0001f469\u200d\u2764\ufe0f\u200d\U0001f48b\u200d\U0001f468", tokenizer="qwen3") == 16


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


def test_tagged_model_reaches_the_decision_through_the_real_entry_point() -> None:
    """The whole point of the seam, pinned end to end.

    A tagged model must produce a decision number that the pure byte estimate
    could not: CJK is where the two rulers diverge hardest (bytes/4 is ~2x the
    real count). If the tokenizer ever stops being resolved inside
    ``assemble_provider_context`` — or stops being forwarded to the decision —
    the tagged and untagged decisions collapse to the same number and this fails.
    """
    payload = "\u8fd9\u662f\u4e00\u4e2a\u4e2d\u6587\u6d4b\u8bd5" * 60  # CJK: 1440 utf8 bytes

    def decide(provider: str, model: str) -> int:
        metadata: dict[str, object] = {
            "runtime_config": {"model": model, "resolved_provider": {"active_target": {"provider": provider, "raw_model": model}}},
        }
        view = assemble_provider_context(
            prompt=payload,
            tool_results=(),
            session_metadata=metadata,
            compaction_budget=CompactionBudget(context_window=1_000_000, anchor_tokens=0, fit_payload=False),
        )
        # ``usage_tokens_before`` is the decision number itself
        # (``max(anchor, estimate)``); ``after`` is the post-assembly report.
        return view.context_window.usage_tokens_before or 0

    tagged = decide("kilo", "qwen/qwen3.6-27b")  # catalog tokenizer: qwen3
    untagged = decide("openai", "gpt-5-model-absent-from-the-catalog")

    # Both consume the identical assembled payload; only the ruler differs. The
    # byte ruler over-counts this CJK payload by well over the rounding margin,
    # so the two decisions cannot coincide unless the tokenizer vanished.
    assert tagged != untagged
    assert tagged < untagged * 0.8


def test_claude_encodings_count_for_real_not_the_byte_fallback() -> None:
    """The four Claude encodings must reach their own engine, never bytes/4.

    ``claude-v3`` is where the two rulers diverge most on this payload: the
    engine counts CJK runs one token per three-character piece (49 here), while
    the byte estimate says 36. A fallback would land on 36, so this fails if the
    CTOK container ever stops loading or the seam stops routing to it.

    The expected numbers were read off oh-my-pi's native ``countTokens`` and are
    reproduced by the shipped vocabulary, so they pin the count, not just its
    presence.
    """
    cjk = "\u4f60\u597d\u4e16\u754c" * 12
    assert count_tokens(cjk, tokenizer="claude-v3") == 49
    assert count_tokens(cjk, tokenizer="claude-v47") == 49
    assert count_tokens(cjk, tokenizer="claude-v5") == 48
    assert count_tokens(cjk, tokenizer="claude-v5-sonnet") == 48
    # The byte estimate these must not collapse onto.
    assert count_tokens(cjk, tokenizer=None) == 36

    # Spacing is the other Claude-specific behaviour: the v3/v4.7 frame ends in
    # a ⟨bow⟩ that absorbs one leading space, so a leading space is free, while
    # two cost one.
    assert count_tokens("the", tokenizer="claude-v3") == 1
    assert count_tokens(" the", tokenizer="claude-v3") == 1
    assert count_tokens("  the", tokenizer="claude-v3") == 2
