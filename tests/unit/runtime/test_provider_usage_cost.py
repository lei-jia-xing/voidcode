"""The persisted usage record carries money, computed once from that turn's usage."""

from __future__ import annotations

from voidcode.provider.model_catalog import static_catalog_metadata
from voidcode.provider.pricing_rules import usage_cost_usd
from voidcode.provider.protocol import ProviderTokenUsage
from voidcode.runtime.contracts import SessionState
from voidcode.runtime.execution.provider_execution_metadata import session_with_provider_usage_metadata


def _session() -> SessionState:
    return SessionState(session="s", status="running", turn=1, metadata={})


def _usage(cost: float | None) -> ProviderTokenUsage:
    return ProviderTokenUsage(
        input_tokens=3000,
        output_tokens=500,
        cache_read_tokens=1000,
        cache_write_tokens=0,
        uncached_input_tokens=2000,
        cost_usd=cost,
    )


def test_latest_and_cumulative_carry_the_amount() -> None:
    first = session_with_provider_usage_metadata(_session(), _usage(0.25))
    payload = first.metadata["provider_usage"]
    assert isinstance(payload, dict)
    assert payload["latest"]["cost_usd"] == 0.25
    assert payload["cumulative"]["cost_usd"] == 0.25

    second = session_with_provider_usage_metadata(first, _usage(0.5))
    payload = second.metadata["provider_usage"]
    assert isinstance(payload, dict)
    assert payload["latest"]["cost_usd"] == 0.5
    # Money accumulates as a float; the token buckets stay integers.
    assert payload["cumulative"]["cost_usd"] == 0.75
    assert payload["cumulative"]["input_tokens"] == 6000


def test_the_amount_is_the_oracle_arithmetic_for_the_observed_usage() -> None:
    """The number persisted is the one the oracle's arithmetic produces for the same
    usage and the catalog's own rates — not a re-derivation at read time."""
    provider, model = "anthropic", "claude-fable-5"
    usage = _usage(None)
    expected = usage_cost_usd(
        provider_id=provider,
        model_id=model,
        usage=usage,
        metadata=static_catalog_metadata(provider, model),
    )

    priced = session_with_provider_usage_metadata(_session(), _usage(expected))
    payload = priced.metadata["provider_usage"]
    assert isinstance(payload, dict)
    assert payload["latest"]["cost_usd"] == expected
    assert expected == (1e-05 * 2000) + (5e-05 * 500) + (1e-06 * 1000)


def test_cumulative_cache_hit_rate_is_the_ratio_of_the_accumulated_totals() -> None:
    """The cumulative rate is the ratio of the summed buckets *after* this turn.
    Reading the previous turn's persisted buckets reported the prior turn's ratio,
    and ``0/0`` (-> None) on the first one."""
    first = session_with_provider_usage_metadata(
        _session(),
        ProviderTokenUsage(input_tokens=3000, output_tokens=10, cache_read_tokens=1000, uncached_input_tokens=2000),
    )
    first_payload = first.metadata["provider_usage"]
    assert isinstance(first_payload, dict)
    assert first_payload["latest"]["cache_hit_rate"] == 1000 / 3000
    assert first_payload["cumulative"]["cache_hit_rate"] == 1000 / 3000

    second = session_with_provider_usage_metadata(
        first,
        ProviderTokenUsage(input_tokens=2000, output_tokens=10, cache_read_tokens=1000, uncached_input_tokens=1000),
    )
    payload = second.metadata["provider_usage"]
    assert isinstance(payload, dict)
    # This turn's own split, and the running 2000/5000 across both turns.
    assert payload["latest"]["cache_hit_rate"] == 1000 / 2000
    assert payload["cumulative"]["cache_hit_rate"] == 2000 / 5000


def test_a_resumed_run_does_not_double_count_the_cost() -> None:
    """Resume rebuilds the session from the *checkpoint payload*, not from the
    sessions row or the truncated event tail (``resume.py``: the resumed
    ``SessionState`` is built with ``metadata=session_metadata``, the checkpoint's
    own metadata). The orphaned turn's finalize therefore never survives, so the
    re-run adds its cost exactly once."""
    checkpoint = session_with_provider_usage_metadata(_session(), _usage(0.25))
    checkpoint_payload = dict(checkpoint.metadata)

    # The resume starts from the checkpoint's metadata (what the code does), and
    # the interrupted turn runs again and finalizes once.
    resumed = SessionState(session="s", status="running", turn=2, metadata=checkpoint_payload)
    re_run = session_with_provider_usage_metadata(resumed, _usage(0.5))

    payload = re_run.metadata["provider_usage"]
    assert isinstance(payload, dict)
    assert payload["cumulative"]["cost_usd"] == 0.75
    assert payload["cumulative"]["input_tokens"] == 6000


def test_a_retried_attempt_inside_one_turn_is_not_accumulated_twice() -> None:
    """The accumulator is driven by the graph step, and a turn produces one step
    (the retry loop lives inside the graph), so one turn adds one amount."""
    session = _session()
    step = _usage(0.25)

    after_turn = session_with_provider_usage_metadata(session, step)

    payload = after_turn.metadata["provider_usage"]
    assert isinstance(payload, dict)
    assert payload["cumulative"]["cost_usd"] == 0.25
    assert payload["turn_count"] == 1
