"""Owned provider clients close through routing; borrowed clients remain usable."""

from types import SimpleNamespace

import pytest

from voidcode.capabilities.providers.dispatch import RegisteredProvider
from voidcode.capabilities.providers.native.anthropic_native import AnthropicMessagesProvider, AnthropicMessagesTransport
from voidcode.capabilities.providers.native.config import ProviderConfigs
from voidcode.capabilities.providers.native.google_native import GoogleGenAIProvider
from voidcode.capabilities.providers.native.model_routing import RoutedTurnProvider, WireRouting
from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider, OpenAIChatCompletionsTransport


@pytest.mark.parametrize("provider_type", [OpenAIChatCompletionsProvider, AnthropicMessagesProvider])
def test_provider_closes_real_sdk_pool(provider_type):
    provider = provider_type()
    client = provider._transport()._sdk()
    assert not client.is_closed()
    provider.close()
    assert client.is_closed()
    provider.close()


@pytest.mark.parametrize("transport_type", [OpenAIChatCompletionsTransport, AnthropicMessagesTransport])
def test_transport_does_not_close_borrowed_http_pool(transport_type):
    import httpx2

    with httpx2.Client() as pool:
        transport = transport_type(http_client=pool)
        transport._sdk()
        transport.close()
        assert not pool.is_closed


def test_google_only_closes_owned_client():
    closed = []
    borrowed = SimpleNamespace(close=lambda: closed.append("borrowed"))
    provider = GoogleGenAIProvider(client=borrowed)
    provider.close()
    assert closed == []
    provider = GoogleGenAIProvider()
    provider._owned_client.value = SimpleNamespace(close=lambda: closed.append("owned"))
    provider.close()
    provider.close()
    assert closed == ["owned"]


@pytest.mark.parametrize("routed", [False, True])
def test_close_releases_all_cached_providers_even_after_failure(routed):
    closed = []

    def fail():
        closed.append("failed")
        raise OSError("close failed")

    if routed:
        provider = RoutedTurnProvider("gateway", WireRouting("gateway", {}, "chat"), lambda *_: None)
    else:
        provider = RegisteredProvider("openai", ProviderConfigs())
    provider._providers.update({"good": SimpleNamespace(close=lambda: closed.append("good")), "bad": SimpleNamespace(close=fail)})
    with pytest.raises(OSError, match="close failed"):
        provider.close()
    assert set(closed) == {"failed", "good"}
    provider.close()
    assert len(closed) == 2
