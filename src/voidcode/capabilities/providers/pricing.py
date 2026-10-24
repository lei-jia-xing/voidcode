"""Price provider-reported usage once, from the shipped catalog.

Cost is attached at report time and never repriced: the core persists the
value it receives. An unknown count or rate stays ``None`` rather than becoming
a guessed zero.
"""

from __future__ import annotations

from dataclasses import replace

from voidcode.sdk.provider import ProviderStreamEvent, ProviderTokenUsage, ProviderTurnRequest, ProviderTurnResult

from .native.model_catalog import ProviderModelMetadata, static_catalog_metadata
from .native.naming import canonical_provider_id


def price_usage(*, provider_name: str, model_name: str, usage: ProviderTokenUsage | None) -> ProviderTokenUsage | None:
    """The same usage with its catalog cost attached, or unchanged when unknown."""
    if usage is None or usage.cost_usd is not None:
        return usage
    if not provider_name or not model_name:
        return usage
    provider_id = canonical_provider_id(provider_name)
    return replace(
        usage,
        cost_usd=usage_cost_usd(
            usage=usage,
            metadata=static_catalog_metadata(provider_id, model_name),
        ),
    )


def price_result(request: ProviderTurnRequest, result: ProviderTurnResult) -> ProviderTurnResult:
    """Attach the catalog cost to one complete turn's usage."""
    priced = price_usage(provider_name=request.provider_name or "", model_name=request.model_name or "", usage=result.usage)
    return result if priced is result.usage else replace(result, usage=priced)


def price_event(request: ProviderTurnRequest, event: ProviderStreamEvent) -> ProviderStreamEvent:
    """Attach the catalog cost to a stream event that carries usage."""
    priced = price_usage(provider_name=request.provider_name or "", model_name=request.model_name or "", usage=event.usage)
    return event if priced is event.usage else replace(event, usage=priced)


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
    rates = {
        "input": metadata.cost_per_input_token,
        "output": metadata.cost_per_output_token,
        "cache_read": metadata.cost_per_cache_read_token,
        "cache_write": metadata.cost_per_cache_write_token,
    }
    if metadata.context_price_tiers:
        uncached = _uncached_input_tokens(usage)
        prompt_input = usage.input_tokens
        if prompt_input is None and uncached is not None and usage.cache_read_tokens is not None:
            prompt_input = uncached + usage.cache_read_tokens
        if prompt_input is None or usage.cache_write_tokens is None:
            return None
        prompt_input += usage.cache_write_tokens
        for tier in sorted(metadata.context_price_tiers, key=lambda tier: tier.threshold):
            if prompt_input > tier.threshold:
                for bucket in rates:
                    override = getattr(tier, bucket)
                    if override is not None:
                        rates[bucket] = override

    costs = (
        _bucket_cost(_uncached_input_tokens(usage), rates["input"]),
        _bucket_cost(usage.output_tokens, rates["output"]),
        _bucket_cost(usage.cache_read_tokens, rates["cache_read"]),
        _bucket_cost(usage.cache_write_tokens, rates["cache_write"]),
    )
    return None if any(cost is None for cost in costs) else sum(cost for cost in costs if cost is not None)
