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


@pytest.mark.parametrize("layer", ["registry", "routing", "google", "openai_sdk", "anthropic_sdk", "openai_transport", "anthropic_transport"])
def test_parallel_first_use_keeps_one_owned_resource(monkeypatch, layer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep

    from voidcode.capabilities.providers import dispatch
    from voidcode.capabilities.providers.native import anthropic_native, google_native, openai_native
    from voidcode.capabilities.providers.native.model_routing import ModelRoute
    from voidcode.sdk.provider import ProviderTurnRequest

    created = []
    closed = []

    def build(*args, **kwargs):
        value = SimpleNamespace(close=lambda: closed.append(1))
        created.append(value)
        sleep(0.03)
        return value

    if layer == "registry":
        monkeypatch.setattr(dispatch, "_materialize", build)
        provider = RegisteredProvider("openai", ProviderConfigs())

        def action():
            return provider._provider_for("openai")
    elif layer == "routing":
        route = ModelRoute("openai-chat-completions")
        provider = RoutedTurnProvider("gateway", WireRouting("gateway", {"chat": route}, "chat"), build)
        request = ProviderTurnRequest(model_name="model", messages=(), available_tools=())

        def action():
            return provider._dispatch(request)[0]
    elif layer == "google":
        monkeypatch.setattr(google_native.GoogleGenAIProvider, "_create_client", build)
        provider = GoogleGenAIProvider()

        def action():
            return provider._client(provider_name="google", model_name="model")
    elif layer.endswith("sdk"):
        module, sdk, factory = (
            (openai_native, "OpenAI", OpenAIChatCompletionsTransport)
            if layer == "openai_sdk"
            else (anthropic_native, "Anthropic", AnthropicMessagesTransport)
        )
        monkeypatch.setattr(module, sdk, build)
        provider = factory()
        action = provider._sdk
    else:
        module, transport, factory = (
            (openai_native, "OpenAIChatCompletionsTransport", OpenAIChatCompletionsProvider)
            if layer == "openai_transport"
            else (anthropic_native, "AnthropicMessagesTransport", AnthropicMessagesProvider)
        )
        monkeypatch.setattr(module, transport, build)
        provider = factory()
        action = provider._transport

    barrier = Barrier(8)

    def invoke(_):
        barrier.wait(timeout=5)
        return action()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(invoke, range(8)))
    assert len(created) == 1
    assert all(result is created[0] for result in results)
    provider.close()
    assert closed == [1]


def test_gateway_defaults_preserve_organization_and_project():
    from voidcode.capabilities.providers.dispatch import _materialize
    from voidcode.capabilities.providers.native.config import ProviderEndpointConfig

    provider = _materialize("openrouter", ProviderEndpointConfig(openai_organization="organization", openai_project="project"))
    try:
        client = provider._transport()._sdk()
        assert client.organization == "organization"
        assert client.project == "project"
    finally:
        provider.close()


def test_unimplemented_upstream_protocol_does_not_fall_back_to_chat(monkeypatch):
    from voidcode.capabilities.providers.native import model_routing
    from voidcode.capabilities.providers.native.model_routing import ModelRoute
    from voidcode.sdk.provider import ProviderExecutionError, ProviderTurnRequest

    monkeypatch.setattr(model_routing, "static_catalog_metadata", lambda *_: SimpleNamespace(api="openai-responses"))

    def build(*_):
        raise AssertionError("unsupported wire must fail before client construction")

    provider = RoutedTurnProvider("gateway", WireRouting("gateway", {"chat": ModelRoute("openai-chat-completions")}, "chat"), build)
    with pytest.raises(ProviderExecutionError) as caught:
        provider.propose_turn(ProviderTurnRequest(messages=(), model_name="new-model"))
    assert caught.value.kind == "unsupported_feature"
