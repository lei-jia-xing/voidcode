from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CacheTTL = Literal["5m", "1h"]


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    supports_anthropic_cache_control: bool = False
    default_cache_ttl: CacheTTL | None = None
    reports_cache_metadata: bool = False


def detect_capability(model: str, provider_config: object | None) -> ProviderCapability:
    normalized_model = _normalize_text(model)
    provider_hint = _provider_hint(provider_config)

    if _is_anthropic_like(normalized_model, provider_hint):
        return ProviderCapability(
            supports_anthropic_cache_control=True,
            default_cache_ttl="5m",
            reports_cache_metadata=True,
        )
    if _is_deepseek_like(normalized_model, provider_hint):
        return ProviderCapability(reports_cache_metadata=True)
    return ProviderCapability()


def _normalize_text(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def _provider_hint(provider_config: object | None) -> str:
    if provider_config is None:
        return ""

    values: list[str] = []
    for name in ("name", "provider", "provider_name"):
        value = getattr(provider_config, name, None)
        if isinstance(value, str):
            values.append(value)
    values.append(type(provider_config).__name__)
    return _normalize_text(" ".join(values))


def _is_anthropic_like(model: str, provider_hint: str) -> bool:
    provider, _, model_name = model.partition("/")
    return (
        provider == "anthropic"
        or "claude" in model_name
        or "claude" in provider
        or "anthropic" in provider_hint
    )


def _is_deepseek_like(model: str, provider_hint: str) -> bool:
    provider, _, model_name = model.partition("/")
    return (
        provider == "deepseek"
        or "deepseek" in model_name
        or "deepseek" in provider
        or "deepseek" in provider_hint
    )
