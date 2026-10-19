"""Price provider-reported usage once, from the shipped catalog.

Cost is attached at report time and never repriced: the kernel persists the
value it receives. An unknown count or rate stays ``None`` rather than becoming
a guessed zero.
"""

from __future__ import annotations

from dataclasses import replace

from voidcode.sdk.provider import ProviderStreamEvent, ProviderTokenUsage, ProviderTurnRequest, ProviderTurnResult

from .native.model_catalog import static_catalog_metadata
from .native.naming import canonical_provider_id
from .native.pricing_rules import usage_cost_usd


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
            provider_id=provider_id,
            model_id=model_name,
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
