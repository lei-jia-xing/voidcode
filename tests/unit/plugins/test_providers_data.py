"""Data invariants for the policy tables the providers plugin ships."""

from __future__ import annotations

import pytest

from voidcode.capabilities.providers.native.api_routes import api_route_for
from voidcode.capabilities.providers.native.model_catalog import static_catalog_metadata
from voidcode.capabilities.providers.native.pricing_rules import usage_cost_usd
from voidcode.capabilities.providers.native.provider_table import PROVIDER_TABLE
from voidcode.capabilities.providers.native.thinking_rules import thinking_rule_for
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


def test_copilot_claude_models_are_pinned_to_the_anthropic_wire() -> None:
    route = api_route_for("github-copilot", "claude-sonnet-4-5")

    assert route is not None
    assert route.api == "anthropic-messages"


def test_usage_cost_oracle_and_unknown_provider_stays_unknown() -> None:
    usage = ProviderTokenUsage(input_tokens=1000, output_tokens=500, cache_read_tokens=100, uncached_input_tokens=900)

    priced = usage_cost_usd(
        provider_id="zai",
        model_id="glm-4.5",
        usage=usage,
        metadata=static_catalog_metadata("zai", "glm-4.5"),
    )

    assert priced == pytest.approx(0.001651)
    assert (
        usage_cost_usd(
            provider_id="deepseek",
            model_id="deepseek-chat",
            usage=usage,
            metadata=static_catalog_metadata("deepseek", "deepseek-chat"),
        )
        is None
    )


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["rates", "multiplier"])
def test_pricing_rules_reject_nonfinite_rates(number: float, field: str) -> None:
    from voidcode.capabilities.providers.native.pricing_rules import _row

    row = {"provider": "openai", "match": {"type": "prefix", "value": "model"}, "threshold": 100}
    row[field] = {"input": number, "output": 1, "cache_read": 1, "cache_write": 1} if field == "rates" else number
    with pytest.raises(ValueError, match="must be a .*number"):
        _row(row)


def test_long_context_prices_respect_threshold_and_unknown_counts() -> None:
    from unittest.mock import patch

    from voidcode.capabilities.providers.native import pricing_rules
    from voidcode.capabilities.providers.native.model_catalog import ProviderModelMetadata
    from voidcode.capabilities.providers.native.pricing_rules import LongContextRates, PricingRuleRow

    metadata = ProviderModelMetadata(cost_per_input_token=1, cost_per_output_token=0, cost_per_cache_read_token=0, cost_per_cache_write_token=0)
    rule = PricingRuleRow("openai", "prefix", "model", 100, False, LongContextRates(2, 0, 0, 0), None)
    with patch.dict(pricing_rules.PRICING_RULES, {"openai": (rule,)}):

        def cost(count, cache_write):
            return usage_cost_usd(
                provider_id="openai",
                model_id="model",
                metadata=metadata,
                usage=ProviderTokenUsage(input_tokens=count, cache_read_tokens=0, cache_write_tokens=cache_write, output_tokens=0),
            )

        assert cost(100, 0) == 100
        assert cost(101, 0) == 202
        assert cost(100, None) is None


def test_malformed_budget_tables_are_not_discarded(monkeypatch) -> None:
    from voidcode.capabilities.providers.native import thinking_rules

    monkeypatch.setattr(thinking_rules, "load_json", lambda _: {"rows": [], "budget_tables": []})
    with pytest.raises(ValueError, match="budget_tables must be an object"):
        thinking_rules._load()
