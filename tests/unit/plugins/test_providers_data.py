"""Data invariants for the policy tables the providers plugin ships."""

from __future__ import annotations

import pytest

from voidcode.capabilities.providers.native.model_catalog import ProviderModelMetadata, static_catalog_metadata
from voidcode.capabilities.providers.native.thinking_rules import thinking_rule_for
from voidcode.capabilities.providers.pricing import usage_cost_usd
from voidcode.capabilities.providers.registry import PROVIDER_TABLE
from voidcode.sdk.provider import ProviderTokenUsage

_SUPPORTED_WIRES = frozenset({"openai-completions", "anthropic-messages", "google-generative-ai"})


def test_provider_table_ids_are_unique_and_every_row_has_an_endpoint() -> None:
    ids = [row.id for row in PROVIDER_TABLE]

    assert len(ids) == len(set(ids))
    assert all(row.id == row.id.lower() for row in PROVIDER_TABLE)
    assert {row.wire for row in PROVIDER_TABLE} <= _SUPPORTED_WIRES
    assert all(row.default_base_url.startswith(("http://", "https://")) for row in PROVIDER_TABLE)
    assert all(row.env_vars for row in PROVIDER_TABLE)


def test_deepseek_requires_authentic_reasoning_for_tool_calls() -> None:
    rule = thinking_rule_for("deepseek", "deepseek-chat")

    assert rule.requires_reasoning_content_for_tool_calls is True
    assert rule.reasoning_content_field == "reasoning_content"
    assert rule.max_tokens_field_or_default() == "max_tokens"


def test_usage_cost_oracle_and_unknown_provider_stays_unknown() -> None:
    usage = ProviderTokenUsage(input_tokens=1000, output_tokens=500, cache_read_tokens=100, uncached_input_tokens=900)

    priced = usage_cost_usd(
        usage=usage,
        metadata=ProviderModelMetadata(
            cost_per_input_token=1e-6, cost_per_output_token=2e-6, cost_per_cache_read_token=0.5e-6, cost_per_cache_write_token=0
        ),
    )

    assert priced == pytest.approx(0.00195)
    assert (
        usage_cost_usd(
            usage=usage,
            metadata=static_catalog_metadata("deepseek", "deepseek-chat"),
        )
        is None
    )


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf"), -1, True])
def test_context_tiers_reject_invalid_rates(number) -> None:
    from voidcode.capabilities.providers.native.model_catalog import ContextPriceTier

    with pytest.raises(ValueError):
        ContextPriceTier(threshold=100, input=number)


def test_long_context_prices_respect_threshold_and_unknown_counts() -> None:
    from voidcode.capabilities.providers.native.model_catalog import ContextPriceTier, ProviderModelMetadata

    metadata = ProviderModelMetadata(
        cost_per_input_token=1,
        cost_per_output_token=0,
        cost_per_cache_read_token=0,
        cost_per_cache_write_token=0,
        context_price_tiers=(ContextPriceTier(100, input=2), ContextPriceTier(200, input=3)),
    )

    def cost(count, cache_write):
        return usage_cost_usd(
            metadata=metadata, usage=ProviderTokenUsage(input_tokens=count, cache_read_tokens=0, cache_write_tokens=cache_write, output_tokens=0)
        )

    assert cost(100, 0) == 100
    assert cost(101, 0) == 202
    assert cost(201, 0) == 603
    assert cost(100, None) is None


def test_malformed_budget_tables_are_not_discarded(monkeypatch) -> None:
    from voidcode.capabilities.providers.native import thinking_rules

    monkeypatch.setattr(thinking_rules, "load_json", lambda _: {"rows": [], "budget_tables": []})
    with pytest.raises(ValueError, match="budget_tables must be an object"):
        thinking_rules._load()


def test_packaged_catalog_preserves_declared_capabilities() -> None:
    from voidcode.capabilities.providers.native.model_catalog import _static_catalog_from_payload

    fields = {
        "supports_streaming": False,
        "supports_json_mode": True,
        "supports_reasoning_summary": True,
        "supports_thinking_budget": False,
        "supports_interleaved_reasoning": True,
        "reasoning_visibility": "visible",
    }
    metadata = _static_catalog_from_payload({"endpoint": {"model": fields}})["endpoint"]["model"]
    assert metadata.payload() == fields


@pytest.mark.parametrize("payload", [{"endpoint": []}, {"endpoint": {"model": []}}])
def test_packaged_catalog_rejects_malformed_rows(payload) -> None:
    from voidcode.capabilities.providers.native.model_catalog import _static_catalog_from_payload

    with pytest.raises(ValueError, match="must .*object"):
        _static_catalog_from_payload(payload)
