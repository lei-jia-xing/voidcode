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

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files as _resource_files
from typing import Final, cast

from .model_catalog import ProviderModelMetadata
from .model_match import MATCHERS, Matcher, matches
from .protocol import ProviderTokenUsage
from .provider_table import require_provider_id

_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class LongContextRates:
    """The rate card one model's tier applies above its threshold.

    USD per token, the unit the catalog's own ``cost_per_*`` fields use; the JSON
    authors the OMP KDL numbers verbatim (USD per 1M) and they are converted once,
    on load.
    """

    input: float
    output: float
    cache_read: float
    cache_write: float


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
            input=(metadata.cost_per_input_token or 0.0) * self.multiplier,
            output=(metadata.cost_per_output_token or 0.0) * self.multiplier,
            cache_read=(metadata.cost_per_cache_read_token or 0.0) * self.multiplier,
            cache_write=(metadata.cost_per_cache_write_token or 0.0) * self.multiplier,
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
    if matcher not in MATCHERS:
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
        matcher=cast(Matcher, matcher),
        value=value,
        threshold=threshold,
        inclusive=inclusive,
        rates=rates,
        multiplier=float(multiplier) if multiplier is not None else None,
        source=source if isinstance(source, str) else "",
    )


def _load() -> Mapping[str, tuple[PricingRuleRow, ...]]:
    payload = json.loads(_resource_files("voidcode.provider").joinpath("pricing_rules.json").read_text(encoding="utf-8"))
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


def _uncached_input_tokens(usage: ProviderTokenUsage) -> int:
    """The prompt tokens priced at the input rate.

    A provider that reports only the prompt total (no uncached/cached split) still
    has to be priced: the uncached part is derived from the total minus whatever
    cached buckets were reported, floored at zero.
    """
    if usage.uncached_input_tokens is not None:
        return usage.uncached_input_tokens
    if usage.input_tokens is None:
        return 0
    return max(0, usage.input_tokens - (usage.cache_read_tokens or 0) - (usage.cache_write_tokens or 0))


def usage_cost_usd(
    *,
    provider_id: str,
    model_id: str,
    usage: ProviderTokenUsage,
    metadata: ProviderModelMetadata | None,
) -> float | None:
    """The USD cost of one turn's usage, from the catalog's flat rates plus any tier.

    Every bucket is priced with the same rate card: the tier's when the prompt's
    input tokens reach its threshold, the catalog's flat rates otherwise. A usage
    field the provider did not report counts as zero, except the prompt total,
    which is split by `_uncached_input_tokens`.

    ``None`` means *unpriced*: the shipped catalog describes no rates for this
    model, so no number can be honest. A model the catalog prices at zero is a
    free model and returns ``0.0`` -- the two are deliberately distinguishable.
    """
    if metadata is None:
        return None
    rates = LongContextRates(
        input=metadata.cost_per_input_token or 0.0,
        output=metadata.cost_per_output_token or 0.0,
        cache_read=metadata.cost_per_cache_read_token or 0.0,
        cache_write=metadata.cost_per_cache_write_token or 0.0,
    )
    uncached = _uncached_input_tokens(usage)
    cache_read = usage.cache_read_tokens or 0
    cache_write = usage.cache_write_tokens or 0
    output = usage.output_tokens or 0
    rule = long_context_rule_for(provider_id, model_id)
    if rule is not None:
        prompt_input = uncached + cache_read + cache_write
        reached = prompt_input > rule.threshold or (rule.inclusive and prompt_input == rule.threshold)
        if reached:
            tier = rule.tier_for(metadata)
            if tier is not None:
                rates = tier
    return rates.input * uncached + rates.output * output + rates.cache_read * cache_read + rates.cache_write * cache_write


__all__ = [
    "PRICING_RULES",
    "LongContextRates",
    "PricingRuleRow",
    "long_context_rule_for",
    "usage_cost_usd",
]
