from __future__ import annotations

from voidcode.provider.config import (
    DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG,
    ProviderConfigs,
    ProviderEndpointConfig,
    ProviderTransientRetryConfig,
)
from voidcode.runtime.execution.provider_fallback import provider_transient_retry_config


def _endpoint_providers() -> ProviderConfigs:
    return ProviderConfigs(endpoint=ProviderEndpointConfig(transient_retry=ProviderTransientRetryConfig(max_retries=7)))


def test_provider_transient_retry_config_reads_endpoint_provider_config() -> None:
    retry_config = provider_transient_retry_config(providers=_endpoint_providers(), provider_name="endpoint")

    assert retry_config.max_retries == 7


def test_provider_transient_retry_config_uses_default_for_unknown_provider() -> None:
    retry_config = provider_transient_retry_config(providers=_endpoint_providers(), provider_name="unregistered")

    assert retry_config == DEFAULT_PROVIDER_TRANSIENT_RETRY_CONFIG
