"""The W4 cost acceptance oracle: model + usage -> USD, row by row.

Rates come from the shipped catalog; the long-context tier comes from the
checked-in policy rows (OMP's ``long-context-cost`` KDL axis). The arithmetic
for every row is written out in ``.omo/plans/cost-oracle.md``.
"""

from __future__ import annotations

import pytest

from voidcode.provider.model_catalog import static_catalog_metadata
from voidcode.provider.pricing_rules import usage_cost_usd
from voidcode.provider.protocol import ProviderTokenUsage


def _cost(provider: str, model: str, *, uncached: int, cache_read: int = 0, cache_write: int = 0, output: int = 0) -> float:
    return usage_cost_usd(
        provider_id=provider,
        model_id=model,
        usage=ProviderTokenUsage(
            input_tokens=uncached + cache_read + cache_write,
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
        # 1 cheap: flat input + output only.
        pytest.param("groq", "openai/gpt-oss-120b", {"uncached": 10000, "output": 500}, 0.0018, id="groq-cheap"),
        # 2 expensive: flat, both buckets large.
        pytest.param("anthropic", "claude-fable-5", {"uncached": 2000, "output": 1000}, 0.07, id="anthropic-expensive"),
        # 3 cached-heavy: cache_read is priced by its own (tiny) rate.
        pytest.param("deepseek", "deepseek-v4-pro", {"uncached": 4000, "cache_read": 96000, "output": 2000}, 0.003828, id="deepseek-cached"),
        # 4 cache-write bucket.
        pytest.param(
            "qwen", "qwen3.6-plus", {"uncached": 5000, "cache_read": 10000, "cache_write": 2000, "output": 1000}, 0.00725, id="qwen-cache-write"
        ),
        # 5 zero cache rates must not change the input/output arithmetic.
        pytest.param("groq", "llama-3.3-70b-versatile", {"uncached": 8000, "cache_read": 5000, "output": 400}, 0.005036, id="groq-zero-cache-rates"),
        # 6 xai's tier is INCLUSIVE: a prompt exactly at 200000 takes the doubled rates.
        pytest.param("xai", "grok-4.6", {"uncached": 200000, "output": 100}, 0.8012, id="xai-inclusive-threshold-applies"),
        # 7 the same model below the threshold stays flat.
        pytest.param("xai", "grok-4.6", {"uncached": 150000, "output": 100}, 0.3006, id="xai-tier-not-reached"),
        # 8 absolute tier above 272000.
        pytest.param("openai", "gpt-6-astra", {"uncached": 300000, "output": 100}, 6.0075, id="openai-absolute-tier"),
        # 9 the threshold is strict for a row without the inclusive flag.
        pytest.param("openai", "gpt-6-astra", {"uncached": 272000, "output": 100}, 2.725, id="openai-strict-threshold"),
        # 10 regression guard: the mirror's context_over_200k must NOT be used for a model
        # OMP gives no tier (the mirror tier would give 3.0045).
        pytest.param("openai", "gpt-5.5", {"uncached": 300000, "output": 100}, 1.503, id="openai-mirror-tier-ignored"),
        # 11 all-zero rates.
        pytest.param("google", "gemma-4-26b-a4b-it", {"uncached": 5000, "cache_read": 2000, "output": 800}, 0.0, id="google-free-model"),
    ],
)
def test_cost_oracle(provider: str, model: str, usage: dict[str, int], expected: float) -> None:
    assert _cost(provider, model, **usage) == pytest.approx(expected, abs=1e-9)


def test_a_model_with_no_shipped_row_is_unpriced_not_free() -> None:
    """No rates means no number: an undescribed model is unpriced (``None``), which is
    distinguishable from a model the catalog prices at zero."""
    cost = usage_cost_usd(
        provider_id="groq",
        model_id="not-in-any-catalog",
        usage=ProviderTokenUsage(input_tokens=1000, output_tokens=1000, uncached_input_tokens=1000),
        metadata=None,
    )

    assert cost is None
    free = usage_cost_usd(
        provider_id="google",
        model_id="gemma-4-26b-a4b-it",
        usage=ProviderTokenUsage(input_tokens=1000, output_tokens=1000, uncached_input_tokens=1000),
        metadata=static_catalog_metadata("google", "gemma-4-26b-a4b-it"),
    )
    assert free == 0.0


def test_a_prompt_total_without_an_uncached_split_is_still_priced() -> None:
    """A provider that reports only ``input_tokens`` must not be priced on output alone."""
    metadata = static_catalog_metadata("openai", "gpt-5.5")
    assert metadata is not None and metadata.cost_per_input_token is not None

    cost = usage_cost_usd(
        provider_id="openai",
        model_id="gpt-5.5",
        usage=ProviderTokenUsage(input_tokens=300000, output_tokens=0, uncached_input_tokens=None),
        metadata=metadata,
    )

    assert cost == pytest.approx(5e-06 * 300000, abs=1e-9)
    # The cached buckets come off the total before the input rate applies.
    with_cache = usage_cost_usd(
        provider_id="openai",
        model_id="gpt-5.5",
        usage=ProviderTokenUsage(input_tokens=300000, cache_read_tokens=100000, uncached_input_tokens=None),
        metadata=metadata,
    )
    assert with_cache == pytest.approx(5e-06 * 200000 + 5e-07 * 100000, abs=1e-9)
