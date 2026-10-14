"""Cost acceptance oracle: model + usage -> USD, row by row.

Catalog rates and long-context tier rules are the production inputs.
"""

from __future__ import annotations

import pytest

from voidcode.provider.model_catalog import ProviderModelMetadata, static_catalog_metadata
from voidcode.provider.pricing_rules import usage_cost_usd
from voidcode.provider.protocol import ProviderTokenUsage


def _cost(provider: str, model: str, *, uncached: int, cache_read: int = 0, cache_write: int = 0, output: int = 0) -> float | None:
    return usage_cost_usd(
        provider_id=provider,
        model_id=model,
        usage=ProviderTokenUsage(
            input_tokens=uncached + cache_read,
            output_tokens=output,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            uncached_input_tokens=uncached,
        ),
        metadata=static_catalog_metadata(provider, model),
    )


@pytest.mark.parametrize(
    ("provider", "model", "usage", "expected"),
    [
        # Cheap: flat input + output only.
        pytest.param("groq", "openai/gpt-oss-120b", {"uncached": 10000, "output": 500}, 0.0018, id="groq-cheap"),
        # Expensive: flat, both buckets large.
        pytest.param("anthropic", "claude-fable-5", {"uncached": 2000, "output": 1000}, 0.07, id="anthropic-expensive"),
        # Cached-heavy: cache_read is priced by its own (tiny) rate.
        pytest.param("deepseek", "deepseek-v4-pro", {"uncached": 4000, "cache_read": 96000, "output": 2000}, 0.008712, id="deepseek-cached"),
        # Cache-write bucket.
        pytest.param(
            "qwen", "qwen3.6-plus", {"uncached": 5000, "cache_read": 10000, "cache_write": 2000, "output": 1000}, 0.00725, id="qwen-cache-write"
        ),
        # An inclusive tier applies at the exact threshold.
        pytest.param("xai", "grok-4.6", {"uncached": 200000, "output": 100}, 0.8012, id="xai-inclusive-threshold-applies"),
        # Below the threshold, flat rates still apply.
        pytest.param("xai", "grok-4.6", {"uncached": 150000, "output": 100}, 0.3006, id="xai-tier-not-reached"),
        # Absolute tier above 272000.
        pytest.param("openai", "gpt-6-astra", {"uncached": 300000, "output": 100}, 6.0075, id="openai-absolute-tier"),
        # A non-inclusive threshold is strict.
        pytest.param("openai", "gpt-6-astra", {"uncached": 272000, "output": 100}, 2.725, id="openai-strict-threshold"),
        # The mirror's context_over_200k must NOT be used for a model with no OMP tier
        # (the mirror tier would give 3.0045).
        pytest.param("openai", "gpt-5.5", {"uncached": 300000, "output": 100}, 1.503, id="openai-mirror-tier-ignored"),
    ],
)
def test_cost_oracle(provider: str, model: str, usage: dict[str, int], expected: float) -> None:
    assert _cost(provider, model, **usage) == pytest.approx(expected, abs=1e-9)


def test_explicit_zero_cache_rates_leave_other_buckets_priced() -> None:
    metadata = ProviderModelMetadata(
        cost_per_input_token=5.9e-7,
        cost_per_output_token=7.9e-7,
        cost_per_cache_read_token=0.0,
        cost_per_cache_write_token=0.0,
    )
    cost = usage_cost_usd(
        provider_id="groq",
        model_id="llama-3.3-70b-versatile",
        usage=ProviderTokenUsage(
            input_tokens=13_000,
            output_tokens=400,
            cache_read_tokens=5_000,
            cache_write_tokens=0,
            uncached_input_tokens=8_000,
        ),
        metadata=metadata,
    )

    assert cost == pytest.approx(0.005036, abs=1e-9)


def test_unknown_rates_are_not_free_and_unknown_usage_is_not_zero() -> None:
    metadata = static_catalog_metadata("google", "gemma-4-26b-a4b-it")
    assert metadata is not None
    cost = usage_cost_usd(
        provider_id="google",
        model_id="gemma-4-26b-a4b-it",
        usage=ProviderTokenUsage(input_tokens=1000, output_tokens=1000, uncached_input_tokens=1000, cache_read_tokens=0, cache_write_tokens=0),
        metadata=metadata,
    )
    assert cost is None

    assert (
        usage_cost_usd(
            provider_id="google",
            model_id="gemma-4-26b-a4b-it",
            usage=ProviderTokenUsage(),
            metadata=metadata,
        )
        is None
    )


def test_missing_priced_usage_bucket_makes_turn_cost_unknown() -> None:
    cost = usage_cost_usd(
        provider_id="groq",
        model_id="openai/gpt-oss-120b",
        usage=ProviderTokenUsage(input_tokens=100, output_tokens=None, cache_read_tokens=0, cache_write_tokens=0, uncached_input_tokens=100),
        metadata=static_catalog_metadata("groq", "openai/gpt-oss-120b"),
    )

    assert cost is None


def test_a_prompt_total_without_a_cache_split_is_not_sufficient_to_price() -> None:
    metadata = static_catalog_metadata("openai", "gpt-5.5")
    assert metadata is not None

    cost = usage_cost_usd(
        provider_id="openai",
        model_id="gpt-5.5",
        usage=ProviderTokenUsage(input_tokens=300000, output_tokens=0, cache_write_tokens=0),
        metadata=metadata,
    )
    assert cost is None
    # With an observed cache split, the same prompt can be priced.
    with_cache = usage_cost_usd(
        provider_id="openai",
        model_id="gpt-5.5",
        usage=ProviderTokenUsage(input_tokens=300000, output_tokens=0, cache_read_tokens=100000, cache_write_tokens=0),
        metadata=metadata,
    )
    assert with_cache == pytest.approx(5e-06 * 200000 + 5e-07 * 100000, abs=1e-9)
