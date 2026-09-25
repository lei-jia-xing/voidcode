"""Measured-anchor budget accounting.

Contract (``docs/contracts/runtime-config.md`` → 预算/阈值): the compaction decision
is the last provider-reported context size plus an estimate of everything added
since, and a present ``measured_anchor_tokens`` says which part was measured.
"""

from __future__ import annotations

from voidcode.runtime.context.window import (
    ContextWindowPolicy,
    estimate_tokens_for_bytes,
    prepare_provider_context,
    provider_usage_anchor_tokens,
)
from voidcode.tools.contracts import ToolResult

PROMPT = "summarize the workspace"
PAYLOAD_BYTES = 4_000


def _result(content: str) -> ToolResult:
    return ToolResult(tool_name="read", status="ok", content=content)


def _policy() -> ContextWindowPolicy:
    return ContextWindowPolicy(
        default_tool_result_chars=None,
        keep_recent_tool_tokens=0,
        min_savings_tokens=1,
        min_prune_tokens=1,
    )


def _window(*, anchor_tokens: int | None, context_window: int | None = 1_000_000, tool_chars: int = 400, prompt: str = PROMPT) -> object:
    return prepare_provider_context(
        prompt=prompt,
        tool_results=(_result("x" * tool_chars),),
        session_metadata={},
        policy=_policy(),
        context_window=context_window,
        payload_bytes=PAYLOAD_BYTES,
        anchor_tokens=anchor_tokens,
    )


# --- anchor + increment --------------------------------------------------------------


def test_the_anchor_moves_the_decision_without_touching_the_increment() -> None:
    unanchored = _window(anchor_tokens=None)
    anchored = _window(anchor_tokens=90_000)

    # Same payload, only the reported context size differs: the decision moves by
    # exactly the anchor, and the estimated part is unchanged.
    assert anchored.usage_tokens_before == unanchored.usage_tokens_before + 90_000
    assert anchored.estimated_delta_tokens == unanchored.estimated_delta_tokens


def test_growth_in_the_payload_moves_only_the_increment() -> None:
    small = _window(anchor_tokens=90_000, tool_chars=400)
    large = _window(anchor_tokens=90_000, tool_chars=4_000)

    assert large.usage_tokens_before - small.usage_tokens_before == estimate_tokens_for_bytes(3_600)


def test_non_ascii_payload_is_not_under_counted() -> None:
    """A character-based ratio would under-count CJK ~3x; the byte basis does not."""

    def window(prompt: str) -> object:
        return prepare_provider_context(
            prompt=prompt,
            tool_results=(),
            session_metadata={},
            policy=_policy(),
            context_window=1_000_000,
            payload_bytes=0,
            anchor_tokens=None,
        )

    ascii_window = window("a" * 300)
    cjk_window = window("中" * 300)

    assert cjk_window.estimated_delta_tokens is not None and ascii_window.estimated_delta_tokens is not None
    assert cjk_window.estimated_delta_tokens >= 2 * ascii_window.estimated_delta_tokens


# --- no usable report ----------------------------------------------------------------


def test_missing_or_zero_usage_falls_back_to_a_pure_estimate() -> None:
    for anchor in (None, 0):
        window = _window(anchor_tokens=anchor)

        # No measured part is reported, so the decision is the estimate alone.
        assert window.measured_anchor_tokens is None
        assert window.usage_tokens_before == window.estimated_delta_tokens


def test_anchor_reader_sums_the_reported_buckets_only_when_present() -> None:
    assert provider_usage_anchor_tokens({}) is None
    assert provider_usage_anchor_tokens({"provider_usage": {"latest": {"input_tokens": 0, "output_tokens": 0}}}) is None
    assert (
        provider_usage_anchor_tokens(
            {"provider_usage": {"latest": {"input_tokens": 1_000, "cache_read_tokens": 9_000, "cache_write_tokens": 500, "output_tokens": 200}}}
        )
        == 10_700
    )
    assert provider_usage_anchor_tokens({"provider_usage": {"latest": {"input_tokens": 5, "output_tokens": None}}}) == 5


# --- the anchor really drives the decision ------------------------------------------


def test_anchor_alone_can_trigger_pruning() -> None:
    without_anchor = _window(anchor_tokens=None, context_window=100_000)
    with_anchor = _window(anchor_tokens=90_000, context_window=100_000)

    assert without_anchor.compacted is False
    assert with_anchor.compacted is True
    assert with_anchor.dropped_tool_result_count == 1
    assert with_anchor.estimated_delta_tokens is not None and with_anchor.estimated_delta_tokens < 90_000
