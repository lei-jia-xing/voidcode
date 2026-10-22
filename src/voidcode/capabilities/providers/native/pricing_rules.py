"""Long-context pricing: the OMP-attested tier rows, and the cost arithmetic they feed.

OMP's tier is a hand-written ``long-context-cost`` KDL axis, not mirror data
(``compat/axes.ts:305``, ``build.ts:143-175``): either absolute rates, or a
``multiplier`` over the model's own base rate. The mirror's ``context_over_200k``
and ``tiers[]`` are deliberately never read — OMP's models.dev row type has no
tier field (``provider-models/openai-compat.ts:121-126``).

Selection follows ``packages/catalog/src/models.ts:60-78``: the prompt's input
tokens are compared against the threshold, strictly greater unless the row is
inclusive; the winning rate card then prices every bucket
(``models.ts:145-152``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from voidcode.sdk.provider import ProviderTokenUsage

from ._resources import load_json
from .model_catalog import ProviderModelMetadata
from .model_match import Matcher, is_matcher, matches
from .provider_table import require_provider_id

_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class LongContextRates:
    """The rate card one model's tier applies above its threshold."""

    input: float | None
    output: float | None
    cache_read: float | None
    cache_write: float | None


@dataclass(frozen=True, slots=True)
class PricingRuleRow:
    """One tier row: what it matches, when it applies, and the rates (or multiplier) it sets."""

    provider: str
    matcher: Matcher
    value: str
    threshold: int
    inclusive: bool
    rates: LongContextRates | None
    multiplier: float | None
    source: str

    def selects(self, model_id: str) -> bool:
        return matches(self.matcher, self.value, model_id)

    def tier_for(self, metadata: ProviderModelMetadata | None) -> LongContextRates | None:
        """The tier's rate card for one model, ``None`` when the row cannot price it.

        A multiplier row scales the model's own base rates, so it needs the
        catalog metadata; an absolute row carries its own numbers.
        """
        if self.rates is not None:
            return self.rates
        if self.multiplier is None or metadata is None:
            return None
        return LongContextRates(
            input=None if metadata.cost_per_input_token is None else metadata.cost_per_input_token * self.multiplier,
            output=None if metadata.cost_per_output_token is None else metadata.cost_per_output_token * self.multiplier,
            cache_read=None if metadata.cost_per_cache_read_token is None else metadata.cost_per_cache_read_token * self.multiplier,
            cache_write=None if metadata.cost_per_cache_write_token is None else metadata.cost_per_cache_write_token * self.multiplier,
        )


def _rates(value: object, *, provider: str, model: str) -> LongContextRates | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"pricing rule for {provider!r}/{model!r} field 'rates' must be an object")
    entry = cast(dict[str, object], value)
    numbers: dict[str, float] = {}
    for key in ("input", "output", "cache_read", "cache_write"):
        raw = entry.get(key)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
            raise ValueError(f"pricing rule for {provider!r}/{model!r} rates.{key} must be a non-negative number")
        numbers[key] = float(raw) / _PER_MILLION
    return LongContextRates(**numbers)


def _row(raw: object) -> PricingRuleRow:
    if not isinstance(raw, dict):
        raise ValueError("pricing rule entries must be objects")
    entry = cast(dict[str, object], raw)
    raw_provider = entry.get("provider")
    if not isinstance(raw_provider, str) or not raw_provider:
        raise ValueError("pricing rule entry is missing a non-empty 'provider'")
    provider = require_provider_id(raw_provider, source="pricing_rules.json")
    match_entry = entry.get("match")
    if not isinstance(match_entry, dict):
        raise ValueError(f"pricing rule for provider {provider!r} is missing a 'match' object")
    match_map = cast(dict[str, object], match_entry)
    matcher = match_map.get("type")
    if not is_matcher(matcher):
        raise ValueError(f"pricing rule for provider {provider!r} has an unknown matcher: {matcher!r}")
    value = match_map.get("value")
    if not isinstance(value, str) or not value:
        raise ValueError(f"pricing rule for provider {provider!r} is missing a non-empty match value")
    threshold = entry.get("threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
        raise ValueError(f"pricing rule for {provider!r}/{value!r} needs a positive integer 'threshold'")
    inclusive = entry.get("inclusive", False)
    if not isinstance(inclusive, bool):
        raise ValueError(f"pricing rule for {provider!r}/{value!r} field 'inclusive' must be a boolean")
    multiplier = entry.get("multiplier")
    if multiplier is not None and (not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool) or multiplier <= 0):
        raise ValueError(f"pricing rule for {provider!r}/{value!r} field 'multiplier' must be a positive number")
    rates = _rates(entry.get("rates"), provider=provider, model=value)
    if rates is None and multiplier is None:
        raise ValueError(f"pricing rule for {provider!r}/{value!r} needs either 'rates' or 'multiplier'")
    source = entry.get("source")
    return PricingRuleRow(
        provider=provider,
        matcher=matcher,
        value=value,
        threshold=threshold,
        inclusive=inclusive,
        rates=rates,
        multiplier=float(multiplier) if multiplier is not None else None,
        source=source if isinstance(source, str) else "",
    )


def _load() -> Mapping[str, tuple[PricingRuleRow, ...]]:
    payload = load_json("pricing_rules.json")
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ValueError("pricing_rules.json must hold a 'rows' list")
    rows: dict[str, list[PricingRuleRow]] = {}
    for raw in cast(list[object], payload["rows"]):
        row = _row(raw)
        rows.setdefault(row.provider, []).append(row)
    return {provider: tuple(provider_rows) for provider, provider_rows in rows.items()}


#: Canonical provider id -> its tier rows, in declaration (first-match-wins) order.
PRICING_RULES: Final[Mapping[str, tuple[PricingRuleRow, ...]]] = _load()


def long_context_rule_for(provider_id: str, model_id: str) -> PricingRuleRow | None:
    """The first tier row that selects ``model_id``, ``None`` when the model has no attested tier."""
    for row in PRICING_RULES.get(provider_id, ()):
        if row.selects(model_id):
            return row
    return None


def _uncached_input_tokens(usage: ProviderTokenUsage) -> int | None:
    if usage.uncached_input_tokens is not None:
        return usage.uncached_input_tokens
    if usage.input_tokens is None or usage.cache_read_tokens is None:
        return None
    return max(0, usage.input_tokens - usage.cache_read_tokens)


def _bucket_cost(count: int | None, rate: float | None) -> float | None:
    if count == 0 or rate == 0:
        return 0.0
    if count is None or rate is None:
        return None
    return count * rate


def usage_cost_usd(
    *,
    provider_id: str,
    model_id: str,
    usage: ProviderTokenUsage,
    metadata: ProviderModelMetadata | None,
) -> float | None:
    """Price observed usage; return ``None`` when a required count or rate is unknown."""
    if metadata is None or all(
        count is None
        for count in (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            usage.uncached_input_tokens,
        )
    ):
        return None
    rates = LongContextRates(
        input=metadata.cost_per_input_token,
        output=metadata.cost_per_output_token,
        cache_read=metadata.cost_per_cache_read_token,
        cache_write=metadata.cost_per_cache_write_token,
    )
    rule = long_context_rule_for(provider_id, model_id)
    if rule is not None:
        uncached = _uncached_input_tokens(usage)
        if usage.input_tokens is not None:
            prompt_input = usage.input_tokens
        elif uncached is not None and usage.cache_read_tokens is not None:
            prompt_input = uncached + usage.cache_read_tokens
        else:
            return None
        if usage.cache_write_tokens is None:
            return None
        prompt_input += usage.cache_write_tokens
        reached = prompt_input > rule.threshold or (rule.inclusive and prompt_input == rule.threshold)
        if reached:
            tier = rule.tier_for(metadata)
            if tier is None:
                return None
            rates = tier

    costs = (
        _bucket_cost(_uncached_input_tokens(usage), rates.input),
        _bucket_cost(usage.output_tokens, rates.output),
        _bucket_cost(usage.cache_read_tokens, rates.cache_read),
        _bucket_cost(usage.cache_write_tokens, rates.cache_write),
    )
    return None if any(cost is None for cost in costs) else sum(cost for cost in costs if cost is not None)


__all__ = [
    "PRICING_RULES",
    "LongContextRates",
    "PricingRuleRow",
    "long_context_rule_for",
    "usage_cost_usd",
]
