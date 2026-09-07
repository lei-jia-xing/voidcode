from __future__ import annotations

from voidcode.provider.model_catalog import DiscoveryRequest, discover_available_models
from voidcode.provider.openrouter import OpenRouterModelProvider


def test_openrouter_discovery_keeps_slash_and_free_model_ids() -> None:
    config = OpenRouterModelProvider().provider_config()
    requests: list[DiscoveryRequest] = []

    def fetcher(request: DiscoveryRequest) -> tuple[str, ...]:
        requests.append(request)
        return (
            "anthropic/claude-3.7-sonnet",
            "meta-llama/llama-3.3-8b-instruct:free",
        )

    result = discover_available_models(
        "openrouter",
        config,
        fetcher=fetcher,
    )
    assert len(requests) == 1
    assert requests[0].base_url == "https://openrouter.ai/api/v1/models"

    assert result.discovery_mode == "configured_endpoint"
    assert result.models == (
        "anthropic/claude-3.7-sonnet",
        "meta-llama/llama-3.3-8b-instruct:free",
    )
