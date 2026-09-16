from __future__ import annotations

import functools
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
from google import genai
from google.genai import types

from voidcode.provider import anthropic_native, openai_native, opencode_go
from voidcode.provider.config import OpenAICompatibleProviderConfig, ProviderEndpointConfig
from voidcode.provider.model_routing import ModelRoute, RoutedTurnProvider, WireRouting
from voidcode.provider.opencode import OpenCodeModelProvider
from voidcode.provider.opencode_go import OpenCodeGoModelProvider
from voidcode.provider.protocol import (
    ProviderAssembledContext,
    ProviderContextSegment,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    StreamableTurnProvider,
    TurnProvider,
)
from voidcode.tools.contracts import ToolResult

_GO_API_KEY = "opencode-go-key"
_GO_ANTHROPIC_MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"
_GO_CHAT_COMPLETIONS_URL = "https://opencode.ai/zen/go/v1/chat/completions"
_ZEN_API_KEY = "opencode-zen-key"
_ZEN_ANTHROPIC_MESSAGES_URL = "https://opencode.ai/zen/v1/messages"
_ZEN_CHAT_COMPLETIONS_URL = "https://opencode.ai/zen/v1/chat/completions"
_ZEN_GENERATE_CONTENT_URL = "https://opencode.ai/zen/v1/models/{model}:generateContent"
_ZEN_STREAM_GENERATE_CONTENT_URL = "https://opencode.ai/zen/v1/models/{model}:streamGenerateContent?alt=sse"
# The official Anthropic SDK is built on ``httpx2``, the OpenAI SDK and
# ``google-genai`` on ``httpx``: each mock client must be the fork its SDK accepts.
_ANTHROPIC_HTTP = httpx2
_OPENAI_HTTP = httpx
_GOOGLE_HTTP = httpx


@dataclass(frozen=True, slots=True)
class _Context:
    prompt: str
    segments: tuple[Any, ...]
    metadata: dict[str, object]
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None


def _request(*, model: str, provider_name: str = "opencode-go", session_id: str | None = None) -> ProviderTurnRequest:
    context = _Context(
        prompt="hello",
        segments=(ProviderContextSegment(role="user", content="hello"),),
        metadata={},
    )
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        provider_name=provider_name,
        model_name=model,
        raw_model=f"{provider_name}/{model}",
        session_id=session_id,
    )


def _tool_request(*, model: str, provider_name: str = "opencode-go") -> ProviderTurnRequest:
    segments = (
        ProviderContextSegment(role="user", content="read a.txt"),
        ProviderContextSegment(role="assistant", content="", tool_name="read", tool_call_id="call-1", tool_arguments={"path": "a.txt"}),
        ProviderContextSegment(
            role="tool",
            content="file body",
            tool_name="read",
            tool_call_id="call-1",
            metadata={"status": "ok", "data": {"path": "a.txt"}},
        ),
    )
    result = ToolResult(
        tool_name="read",
        status="ok",
        content="file body",
        data={"path": "a.txt", "arguments": {"path": "a.txt"}},
    )
    context = _Context(prompt="read a.txt", segments=segments, metadata={}, tool_results=(result,))
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        provider_name=provider_name,
        model_name=model,
        raw_model=f"{provider_name}/{model}",
    )


@dataclass(slots=True)
class _WireCapture:
    """One gateway turn as observed at the official-SDK boundary."""

    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    payload: dict[str, object] = field(default_factory=dict)
    clients_built: int = 0


_GOOGLE_RESPONSE_BODY: dict[str, object] = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}]}
_GOOGLE_STREAM_BODY = "\n\n".join(
    [
        'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"!"}]},"finishReason":"STOP"}]}',
        "",
    ]
)


def _respond(package: Any, kind: str, capture: _WireCapture, request: Any) -> Any:
    capture.url = str(request.url)
    capture.headers = {name.lower(): value for name, value in request.headers.items()}
    body = json.loads(request.content or b"{}")
    capture.payload = cast(dict[str, object], body)
    if kind == "google":
        # google-genai reads a generate-content body, not an OpenAI/Anthropic one.
        if ":streamGenerateContent" in capture.url:
            return package.Response(200, headers={"content-type": "text/event-stream"}, text=_GOOGLE_STREAM_BODY)
        return package.Response(200, json=_GOOGLE_RESPONSE_BODY)
    if body.get("stream"):
        return package.Response(200, headers={"content-type": "text/event-stream"}, text=_stream_body(request))
    return package.Response(200, json=_response_body(request))


def _install_sdk_wire(monkeypatch: pytest.MonkeyPatch, capture: _WireCapture) -> None:
    """Route every official SDK through mock HTTP clients.

    Patching the SDK constructors rather than the VoidCode transports keeps the
    real client, the real base-URL join and the real auth headers inside the
    assertion path.
    """

    for module, class_name, package, kind in (
        (anthropic_native, "Anthropic", _ANTHROPIC_HTTP, "anthropic"),
        (openai_native, "OpenAI", _OPENAI_HTTP, "openai"),
        (genai, "Client", _GOOGLE_HTTP, "google"),
    ):
        real_constructor = getattr(module, class_name)
        http_client = package.Client(
            transport=package.MockTransport(lambda request, _package=package, _kind=kind: _respond(_package, _kind, capture, request))
        )

        def build_client(*args: Any, _real: Any = real_constructor, _client: Any = http_client, _kind: str = kind, **kwargs: Any) -> Any:
            capture.clients_built += 1
            if _kind == "google":
                # The Google SDK takes its HTTP client through ``HttpOptions``, so
                # the provider's own options (base URL, cleared version) survive.
                options = kwargs.pop("http_options", None) or types.HttpOptions()
                kwargs["http_options"] = options.model_copy(update={"httpx_client": _client})
            else:
                kwargs["http_client"] = _client
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, class_name, build_client)


def _response_body(request: Any) -> dict[str, object]:
    body = json.loads(request.content or b"{}")
    if request.url.path.endswith("/messages"):
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
    return {
        "id": "chatcmpl_1",
        "model": body.get("model"),
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def _stream_body(request: Any) -> str:
    body = json.loads(request.content or b"{}")
    if request.url.path.endswith("/messages"):
        return "\n\n".join(
            [
                'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant",'
                '"model":"' + str(body.get("model")) + '","content":[],"usage":{"input_tokens":3,"output_tokens":0}}}',
                'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
                'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
                'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
                'event: message_stop\ndata: {"type":"message_stop"}',
                "",
            ]
        )
    return "\n\n".join(
        [
            'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
            "",
        ]
    )


def _go_turn_provider(config: OpenAICompatibleProviderConfig | None = None) -> Any:
    return OpenCodeGoModelProvider(config=config or OpenAICompatibleProviderConfig(api_key=_GO_API_KEY)).turn_provider()


def _zen_turn_provider(config: ProviderEndpointConfig | None = None) -> Any:
    return OpenCodeModelProvider(config=config or ProviderEndpointConfig(api_key=_ZEN_API_KEY)).turn_provider()


def _trace_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log = tmp_path / "provider-trace.jsonl"
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE", "1")
    monkeypatch.setenv("VOIDCODE_PROVIDER_TRACE_LOG", str(log))
    return log


def _last_trace(log: Path) -> dict[str, object]:
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    return cast(dict[str, object], records[-1]["metadata"])


def test_opencode_go_minimax_m3_speaks_anthropic_messages_at_the_gateway_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    log = _trace_log(monkeypatch, tmp_path)

    result = _go_turn_provider().propose_turn(_request(model="minimax-m3"))

    assert capture.url == _GO_ANTHROPIC_MESSAGES_URL
    assert capture.headers["x-api-key"] == _GO_API_KEY
    assert capture.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in capture.headers
    assert capture.payload["model"] == "minimax-m3"
    assert result.output == "hello"
    assert _last_trace(log)["transport"] == "anthropic_messages"


def test_opencode_go_minimax_m3_streams_over_anthropic_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    events = list(_go_turn_provider().stream_turn(_request(model="minimax-m3")))

    assert capture.url == _GO_ANTHROPIC_MESSAGES_URL
    assert capture.payload["stream"] is True
    assert [event.text for event in events if event.kind == "delta"] == ["hi"]
    assert [event.done_reason for event in events if event.kind == "done"] == ["stop"]


@pytest.mark.parametrize("model", ["glm-5.1", "minimax-m2.7", "qwen3.6-plus", "grok-4.5"])
def test_opencode_go_other_models_stay_on_chat_completions(
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    result = _go_turn_provider().propose_turn(_request(model=model))

    assert capture.url == _GO_CHAT_COMPLETIONS_URL
    assert capture.headers["authorization"] == f"Bearer {_GO_API_KEY}"
    assert "x-api-key" not in capture.headers
    assert capture.payload["model"] == model
    assert result.output == "hello"


def test_opencode_go_chat_completions_stream_reports_the_openai_wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    log = _trace_log(monkeypatch, tmp_path)

    events = list(_go_turn_provider().stream_turn(_request(model="glm-5.1")))

    assert capture.url == _GO_CHAT_COMPLETIONS_URL
    assert [event.text for event in events if event.kind == "delta"] == ["hi"]
    assert [event.done_reason for event in events if event.kind == "done"] == ["stop"]
    assert _last_trace(log)["transport"] == "openai_chat_completions"


def test_opencode_go_aliased_model_uses_the_default_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _go_turn_provider(OpenAICompatibleProviderConfig(api_key=_GO_API_KEY, model_map={"fast": "upstream-unknown-model"}))

    _ = provider.propose_turn(_request(model="fast"))

    assert capture.url == _GO_CHAT_COMPLETIONS_URL
    assert capture.payload["model"] == "upstream-unknown-model"


def test_opencode_go_configured_base_url_wins_for_the_default_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _go_turn_provider(OpenAICompatibleProviderConfig(api_key=_GO_API_KEY, base_url="https://go-proxy.example.test/zen/go"))

    _ = provider.propose_turn(_request(model="glm-5.1"))

    assert capture.url == "https://go-proxy.example.test/zen/go/v1/chat/completions"


def test_opencode_go_chat_route_base_url_overrides_the_provider_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A chat route that pins its own base URL wins over the configured endpoint.
    # The shipping default route pins none, which is what lets user config win.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    monkeypatch.setattr(
        opencode_go,
        "_GO_ROUTING",
        WireRouting(
            default=ModelRoute(wire="openai-chat-completions", base_url="https://go-mirror.example.test/zen/go"),
            overrides={},
        ),
    )
    provider = _go_turn_provider(OpenAICompatibleProviderConfig(api_key=_GO_API_KEY, base_url="https://go-proxy.example.test/zen/go"))

    _ = provider.propose_turn(_request(model="glm-5.1"))

    assert capture.url == "https://go-mirror.example.test/zen/go/v1/chat/completions"


def test_opencode_go_anthropic_route_keeps_the_gateway_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Anthropic route carries its own base URL: a configured chat prefix is
    # not a valid Anthropic root, and the vendor default must never be used for
    # this gateway's credential.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _go_turn_provider(OpenAICompatibleProviderConfig(api_key=_GO_API_KEY, base_url="https://go-proxy.example.test/zen/go"))

    _ = provider.propose_turn(_request(model="minimax-m3"))

    assert capture.url == _GO_ANTHROPIC_MESSAGES_URL


@pytest.mark.parametrize(("model", "expects_tool_role"), [("qwen3.6-plus", False), ("minimax-m2.7", True)])
def test_opencode_go_tool_feedback_modes_follow_the_routing_table(
    model: str,
    expects_tool_role: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    _ = _go_turn_provider().propose_turn(_tool_request(model=model))

    roles = [message["role"] for message in cast(list[dict[str, object]], capture.payload["messages"])]
    assert ("tool" in roles) is expects_tool_role


def test_opencode_go_gpt_5_6_luna_fails_typed_without_opening_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    with pytest.raises(ProviderExecutionError) as excinfo:
        _go_turn_provider().propose_turn(_request(model="gpt-5.6-luna"))

    error = excinfo.value
    assert error.kind == "unsupported_feature"
    assert error.provider_name == "opencode-go"
    assert error.model_name == "gpt-5.6-luna"
    assert error.retryable is False
    assert error.fallback_allowed is True
    assert "gpt-5.6-luna" in error.message
    assert "use another model" in error.message
    assert capture.clients_built == 0
    assert capture.url is None


@pytest.mark.parametrize("model", ["claude-opus-5", "qwen3.6-plus"])
def test_opencode_zen_anthropic_models_speak_anthropic_messages_at_the_gateway_root(
    model: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    log = _trace_log(monkeypatch, tmp_path)

    result = _zen_turn_provider().propose_turn(_request(model=model, provider_name="opencode"))

    assert capture.url == _ZEN_ANTHROPIC_MESSAGES_URL
    assert capture.headers["x-api-key"] == _ZEN_API_KEY
    assert capture.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in capture.headers
    assert capture.payload["model"] == model
    assert result.output == "hello"
    assert _last_trace(log)["transport"] == "anthropic_messages"


@pytest.mark.parametrize("model", ["glm-5.1", "minimax-m3", "deepseek-v4-flash-free"])
def test_opencode_zen_chat_models_stay_on_chat_completions(model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``minimax-m3`` is Anthropic Messages on opencode-go and chat-completions on
    # Zen, and the ``*-free`` model OMP does not list keeps the default route.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    result = _zen_turn_provider().propose_turn(_request(model=model, provider_name="opencode"))

    assert capture.url == _ZEN_CHAT_COMPLETIONS_URL
    assert capture.headers["authorization"] == f"Bearer {_ZEN_API_KEY}"
    assert "x-api-key" not in capture.headers
    assert capture.payload["model"] == model
    assert result.output == "hello"


@pytest.mark.parametrize("model", ["gemini-3-flash", "gemini-3.1-pro"])
def test_opencode_zen_google_models_speak_generate_content_at_the_gateway_base_url(model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    result = _zen_turn_provider().propose_turn(_request(model=model, provider_name="opencode"))

    # The gateway's ``/v1`` prefix is the version segment: the Google SDK must not
    # append its own ``v1beta``.
    assert capture.url == _ZEN_GENERATE_CONTENT_URL.format(model=model)
    assert capture.headers["x-goog-api-key"] == _ZEN_API_KEY
    assert "authorization" not in capture.headers
    assert result.output == "hello"


def test_opencode_zen_google_route_streams_over_stream_generate_content(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    events = list(_zen_turn_provider().stream_turn(_request(model="gemini-3-flash", provider_name="opencode")))

    assert capture.url == _ZEN_STREAM_GENERATE_CONTENT_URL.format(model="gemini-3-flash")
    assert [event.text for event in events if event.kind == "delta"] == ["hi", "!"]
    assert [event.done_reason for event in events if event.kind == "done"] == ["stop"]


@pytest.mark.parametrize("model", ["gpt-5", "grok-4.5", "muse-spark-1.2"])
def test_opencode_zen_responses_models_fail_typed_without_opening_a_connection(model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    with pytest.raises(ProviderExecutionError) as excinfo:
        _zen_turn_provider().propose_turn(_request(model=model, provider_name="opencode"))

    error = excinfo.value
    assert error.kind == "unsupported_feature"
    assert error.provider_name == "opencode"
    assert error.model_name == model
    assert error.retryable is False
    assert error.fallback_allowed is True
    assert "use another model" in error.message
    assert capture.clients_built == 0
    assert capture.url is None


def test_opencode_zen_aliased_model_uses_the_default_route(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _zen_turn_provider(
        ProviderEndpointConfig(api_key=_ZEN_API_KEY, model_map={"fast": "upstream-unknown-model", "claude": "claude-opus-5"})
    )

    _ = provider.propose_turn(_request(model="fast", provider_name="opencode"))

    assert capture.url == _ZEN_CHAT_COMPLETIONS_URL
    assert capture.payload["model"] == "upstream-unknown-model"

    # An alias onto a routed model takes that model's wire, not the default one.
    _ = provider.propose_turn(_request(model="claude", provider_name="opencode"))

    assert capture.url == _ZEN_ANTHROPIC_MESSAGES_URL
    assert capture.payload["model"] == "claude-opus-5"


def test_opencode_zen_configured_base_url_wins_for_the_default_route(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _zen_turn_provider(ProviderEndpointConfig(api_key=_ZEN_API_KEY, base_url="https://zen-proxy.example.test/zen/v1"))

    _ = provider.propose_turn(_request(model="glm-5.1", provider_name="opencode"))

    assert capture.url == "https://zen-proxy.example.test/zen/v1/chat/completions"


def test_opencode_zen_configured_base_url_wins_for_the_google_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Google route is the gateway's own base URL, so user config reaches it.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _zen_turn_provider(ProviderEndpointConfig(api_key=_ZEN_API_KEY, base_url="https://zen-proxy.example.test/zen/v1"))

    _ = provider.propose_turn(_request(model="gemini-3-flash", provider_name="opencode"))

    assert capture.url == "https://zen-proxy.example.test/zen/v1/models/gemini-3-flash:generateContent"


def test_opencode_zen_anthropic_route_keeps_the_gateway_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Anthropic route carries its own base URL: a configured chat prefix is
    # not a valid Anthropic root, and the vendor default must never be used for
    # this gateway's credential.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _zen_turn_provider(ProviderEndpointConfig(api_key=_ZEN_API_KEY, base_url="https://zen-proxy.example.test/zen/v1"))

    _ = provider.propose_turn(_request(model="claude-opus-5", provider_name="opencode"))

    assert capture.url == _ZEN_ANTHROPIC_MESSAGES_URL


def test_opencode_zen_chat_route_keeps_the_tool_role(monkeypatch: pytest.MonkeyPatch) -> None:
    # Zen's chat-completions route accepts the OpenAI ``tool`` role, so the
    # adapter declares no tool-feedback override (unlike opencode-go).
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    _ = _zen_turn_provider().propose_turn(_tool_request(model="glm-5.1", provider_name="opencode"))

    roles = [message["role"] for message in cast(list[dict[str, object]], capture.payload["messages"])]
    assert "tool" in roles


_OPENCODE_GO_URLS = {
    "glm-5.1": _GO_CHAT_COMPLETIONS_URL,
    "minimax-m3": _GO_ANTHROPIC_MESSAGES_URL,
}
_OPENCODE_ZEN_URLS = {
    "deepseek-v4-flash-free": _ZEN_CHAT_COMPLETIONS_URL,
    "claude-opus-5": _ZEN_ANTHROPIC_MESSAGES_URL,
    "gemini-3-flash": _ZEN_GENERATE_CONTENT_URL.format(model="gemini-3-flash"),
}


def _turn_provider_for(gateway: str) -> Any:
    return _go_turn_provider() if gateway == "go" else _zen_turn_provider()


def _gateway_request(gateway: str, model: str, *, session_id: str | None) -> ProviderTurnRequest:
    return _request(model=model, provider_name="opencode-go" if gateway == "go" else "opencode", session_id=session_id)


@pytest.mark.parametrize(
    ("gateway", "model", "expected_url"),
    [(gateway, model, url) for gateway, urls in (("go", _OPENCODE_GO_URLS), ("zen", _OPENCODE_ZEN_URLS)) for model, url in urls.items()],
)
def test_opencode_wires_carry_the_gateway_session_header(
    gateway: str,
    model: str,
    expected_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both gateways reject a turn that does not name its conversation (HTTP 400
    # ``MissingSessionID``), on every wire they serve.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    _ = _turn_provider_for(gateway).propose_turn(_gateway_request(gateway, model, session_id="conversation-1"))

    assert capture.url == expected_url
    assert capture.headers["x-opencode-session"] == "conversation-1"
    assert capture.headers["x-opencode-client"] == "voidcode"
    # The header is a request option, never a body field.
    body = json.dumps(capture.payload)
    assert "x-opencode-session" not in body
    assert "x-opencode-client" not in body


@pytest.mark.parametrize(
    ("gateway", "model"),
    [
        ("go", "glm-5.1"),
        ("go", "minimax-m3"),
        ("zen", "deepseek-v4-flash-free"),
        ("zen", "claude-opus-5"),
        ("zen", "gemini-3-flash"),
    ],
)
def test_opencode_session_header_is_resolved_per_request(
    gateway: str,
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The header names the conversation, and one client is reused across turns:
    # a value resolved once -- at construction, or by the first turn -- would pin
    # every later conversation to the first one's id.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)
    provider = _turn_provider_for(gateway)
    request = functools.partial(_gateway_request, gateway, model)

    _ = provider.propose_turn(request(session_id="conversation-a"))
    first = capture.headers["x-opencode-session"]
    _ = provider.propose_turn(request(session_id="conversation-b"))
    second = capture.headers["x-opencode-session"]
    _ = list(provider.stream_turn(request(session_id="conversation-c")))
    streamed = capture.headers["x-opencode-session"]

    assert (first, second, streamed) == ("conversation-a", "conversation-b", "conversation-c")
    assert capture.headers["x-opencode-client"] == "voidcode"
    # One SDK client for the whole gateway, whatever the conversation.
    assert capture.clients_built == 1


@pytest.mark.parametrize(
    ("gateway", "model", "expected_url"),
    [(gateway, model, url) for gateway, urls in (("go", _OPENCODE_GO_URLS), ("zen", _OPENCODE_ZEN_URLS)) for model, url in urls.items()],
)
def test_opencode_wire_omits_the_session_header_without_a_session_id(
    gateway: str,
    model: str,
    expected_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty header names no conversation, so the turn must not carry one.
    capture = _WireCapture()
    _install_sdk_wire(monkeypatch, capture)

    _ = _turn_provider_for(gateway).propose_turn(_gateway_request(gateway, model, session_id=None))

    assert capture.url == expected_url
    assert "x-opencode-session" not in capture.headers
    assert capture.headers["x-opencode-client"] == "voidcode"


@dataclass(slots=True)
class _RecordingSubProvider:
    """Sub-provider stub: reports the route it was built for on every wire."""

    route: ModelRoute
    name: str = "gateway"

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        return ProviderTurnResult(output=f"{self.route.wire}:{request.model_name}")

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(kind="content", channel="text", text=f"{self.route.wire}:{request.model_name}")
        yield ProviderStreamEvent(kind="done", done_reason="stop")


def _recording_build(built: list[tuple[str, ModelRoute]]) -> Any:
    def build(model: str, route: ModelRoute) -> _RecordingSubProvider:
        built.append((model, route))
        return _RecordingSubProvider(route=route)

    return build


def test_routed_turn_provider_dispatches_per_model_and_caches_sub_providers() -> None:
    routing = WireRouting(
        default=ModelRoute(wire="openai-chat-completions", base_url="https://gateway.test/v1"),
        overrides={"m3": ModelRoute(wire="anthropic-messages", base_url="https://gateway.test")},
    )
    built: list[tuple[str, ModelRoute]] = []
    provider = RoutedTurnProvider(name="gateway", routing=routing, build=_recording_build(built))

    assert provider.name == "gateway"
    # The runtime picks the streaming path by this check, so it is contractual.
    assert isinstance(provider, TurnProvider)
    assert isinstance(provider, StreamableTurnProvider)
    assert provider.propose_turn(_request(model="m3", provider_name="gateway")).output == "anthropic-messages:m3"
    assert provider.propose_turn(_request(model="plain", provider_name="gateway")).output == "openai-chat-completions:plain"
    assert provider.propose_turn(_request(model="m3", provider_name="gateway")).output == "anthropic-messages:m3"
    events = list(provider.stream_turn(_request(model="m3", provider_name="gateway")))

    assert [event.text for event in events if event.kind == "content"] == ["anthropic-messages:m3"]
    assert [event.done_reason for event in events if event.kind == "done"] == ["stop"]
    # One sub-provider per routed model, reused across turns and across wires.
    assert built == [
        ("m3", ModelRoute(wire="anthropic-messages", base_url="https://gateway.test")),
        ("plain", ModelRoute(wire="openai-chat-completions", base_url="https://gateway.test/v1")),
    ]


def test_routed_turn_provider_resolves_model_map_before_routing() -> None:
    default = ModelRoute(wire="openai-chat-completions")
    routed = ModelRoute(wire="anthropic-messages", base_url="https://gateway.test")
    provider = RoutedTurnProvider(
        name="gateway",
        routing=WireRouting(default=default, overrides={"m3": routed}),
        build=_recording_build([]),
        model_map={"fast": "m3", "ghost": "upstream-unknown-model", "blank": ""},
    )

    # An alias resolves to the routed model it names...
    assert provider.route_for(_request(model="fast", provider_name="gateway")) == ("m3", routed)
    # ...an alias naming an unrouted upstream model keeps the default route...
    assert provider.route_for(_request(model="ghost", provider_name="gateway")) == ("upstream-unknown-model", default)
    # ...and an empty mapping is not an alias at all.
    assert provider.route_for(_request(model="blank", provider_name="gateway")) == ("blank", default)
