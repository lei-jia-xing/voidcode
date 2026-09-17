from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx2
import openai
import pytest

from voidcode.provider.config import (
    OpenAICompatibleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    provider_configs_from_env,
)
from voidcode.provider.errors import guidance_for_provider_error_kind
from voidcode.provider.model_catalog import ProviderModelMetadata, _headers_for_discovery, static_catalog_metadata
from voidcode.provider.openai import OpenAIModelProvider
from voidcode.provider.openai_native import OpenAIChatCompletionsProvider, OpenAIChatCompletionsTransport
from voidcode.provider.protocol import (
    ProviderAssembledContext,
    ProviderContextSegment,
    ProviderContextWindow,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from voidcode.provider.registry import ModelProviderRegistry
from voidcode.tools.contracts import ToolDefinition, ToolResult


@dataclass(frozen=True, slots=True)
class _ContextWindow:
    prompt: str
    tool_results: tuple[object, ...] = ()
    compacted: bool = False
    retained_tool_result_count: int = 0
    continuity_state: object | None = None


@dataclass(frozen=True, slots=True)
class _Context:
    prompt: str
    segments: tuple[ProviderContextSegment, ...]
    metadata: dict[str, object]
    tool_results: tuple[object, ...] = ()
    continuity_state: object | None = None


def _request(*, transport: object | None, abort_signal: object | None = None, session_id: str | None = None) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ProviderContextSegment(role="user", content="hello"),), metadata={})
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt="hello")),
        available_tools=(
            ToolDefinition(
                name="read",
                description="read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        provider_name="openai",
        model_name="gpt-4o",
        raw_model="openai/gpt-4o",
        session_id=session_id,
        abort_signal=cast(object, abort_signal),
    )


def test_native_non_stream_uses_chat_completions_wire_and_auth_headers() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "gpt-4o",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    transport = OpenAIChatCompletionsTransport(
        api_key="sk-test",
        organization="org-test",
        project="proj-test",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    result = (
        OpenAIModelProvider(config=OpenAIProviderConfig(api_key="sk-test"), transport=transport)
        .turn_provider()
        .propose_turn(_request(transport=transport))
    )
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    headers = cast(dict[str, str], seen["headers"])
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["openai-organization"] == "org-test"
    assert headers["openai-project"] == "proj-test"
    payload = cast(dict[str, object], seen["payload"])
    assert payload["model"] == "gpt-4o"
    assert payload["stream"] is False
    assert result.output == "hello"
    assert result.done_reason == "stop"
    assert result.usage is not None and result.usage.total_tokens == 10


def test_native_stream_emits_text_tool_lifecycle_and_trailing_usage() -> None:
    body = "\n\n".join(
        [
            'data: {"id":"chatcmpl-2","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
            (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                '"function":{"name":"read","arguments":"{\\"path\\":\\"a"}}]},'
                '"finish_reason":null}]}'
            ),
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":".txt\\"}"}}]},"finish_reason":"tool_calls"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4}}',
            "data: [DONE]",
            "",
        ]
    )

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    events = list(OpenAIModelProvider(transport=transport).turn_provider().stream_turn(_request(transport=transport)))
    assert [(event.kind, event.channel) for event in events] == [
        ("delta", "text"),
        ("tool_call_start", "tool"),
        ("tool_call_delta", "tool"),
        ("tool_call_delta", "tool"),
        ("tool_call_end", "tool"),
        ("done", "text"),
    ]
    assert events[-1].done_reason == "tool_calls"
    assert events[-1].usage is not None and events[-1].usage.output_tokens == 4
    assert events[-2].parsed_arguments == {"path": "a.txt"}


def test_native_stream_abort_before_first_event_never_calls_transport() -> None:
    class _Abort:
        cancelled = True

    class _FailingTransport:
        def request(self, _payload: dict[str, object], *, timeout_seconds: float) -> object:
            raise AssertionError("transport must not be called after abort")

    events = list(
        OpenAIModelProvider(transport=cast(object, _FailingTransport())).turn_provider().stream_turn(_request(transport=None, abort_signal=_Abort()))
    )
    assert events[0].error_kind == "cancelled"
    assert events[-1].done_reason == "cancelled"


def test_native_http_error_is_typed_and_redacted() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"error": {"message": "slow down", "code": "rate_limit"}, "token": "sk-secret"})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(ProviderExecutionError) as raised:
        OpenAIModelProvider(transport=transport).turn_provider().propose_turn(_request(transport=transport))
    assert raised.value.kind == "rate_limit"
    assert "sk-secret" not in repr(raised.value.details)


def _captured_request_headers(**transport_kwargs: object) -> dict[str, str]:
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = dict(http_request.headers)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **cast(Any, transport_kwargs))
    transport.request({"model": "gpt-4o", "messages": [], "stream": False}, timeout_seconds=5.0)
    return {key.lower(): value for key, value in cast(dict[str, str], seen["headers"]).items()}


def _auth_headers_of(headers: dict[str, str]) -> dict[str, str]:
    return {name: headers[name] for name in ("authorization", "x-api-key") if name in headers}


def _wire_tool_names(payload: dict[str, object]) -> list[str]:
    tools = cast(list[dict[str, object]], payload["tools"])
    return [cast(str, cast(dict[str, object], tool["function"])["name"]) for tool in tools]


@pytest.mark.parametrize(
    ("auth_scheme", "auth_header", "api_key", "expected"),
    [
        # `none` -- or a missing key -- sends no credential at all, not an empty one.
        ("none", None, "secret-key", {}),
        ("none", "X-API-Key", "secret-key", {}),
        ("bearer", "Authorization", None, {}),
        # `bearer` always carries the scheme prefix, in whichever header is configured.
        ("bearer", None, "secret-key", {"authorization": "Bearer secret-key"}),
        ("bearer", "Authorization", "secret-key", {"authorization": "Bearer secret-key"}),
        ("bearer", "X-API-Key", "secret-key", {"x-api-key": "Bearer secret-key"}),
        # `token` sends the raw key.
        ("token", None, "secret-key", {"authorization": "secret-key"}),
        ("token", "X-API-Key", "secret-key", {"x-api-key": "secret-key"}),
    ],
)
def test_openai_transport_auth_headers_follow_the_scheme_contract(
    auth_scheme: str, auth_header: str | None, api_key: str | None, expected: dict[str, str]
) -> None:
    headers = _auth_headers_of(_captured_request_headers(api_key=api_key, auth_header=auth_header, auth_scheme=auth_scheme))

    assert headers == expected
    # The model-call wire and the discovery wire must agree for the same config: one
    # scheme, one credential placement. They had drifted (model calls dropped the
    # `Bearer` prefix, ignored `none`, and leaked the key into a second header).
    config = ProviderEndpointConfig(
        base_url="https://gateway.test/v1",
        api_key=api_key,
        auth_header=auth_header,
        auth_scheme=cast(Any, auth_scheme),
    )
    discovery = {name.lower(): value for name, value in _headers_for_discovery(config).items()}
    assert headers == _auth_headers_of(discovery)


def test_openai_transport_respects_request_scoped_authorization_override() -> None:
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = dict(http_request.headers)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(
        base_url="https://gateway.test/v1",
        auth_scheme="none",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    transport.request(
        {"model": "gpt-4o", "messages": [], "stream": False, "extra_headers": {"Authorization": "Token per-request"}},
        timeout_seconds=5.0,
    )

    headers = {key.lower(): value for key, value in cast(dict[str, str], seen["headers"]).items()}
    # Suppressing the SDK default must not clobber an explicit per-request header.
    assert headers["authorization"] == "Token per-request"


def _sdk_over_mock_transport(seen: dict[str, object]) -> Any:
    """Real SDK over a recording MockTransport, so the wire itself is observable."""

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(http_request.url)
        seen["headers"] = dict(http_request.headers)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    real_openai = openai.OpenAI

    def build(**kwargs: object) -> object:
        kwargs["http_client"] = httpx2.Client(transport=httpx2.MockTransport(handler))
        return real_openai(**cast(Any, kwargs))

    return build


def _wire_headers(seen: dict[str, object]) -> dict[str, str]:
    return {key.lower(): value for key, value in cast(dict[str, str], seen["headers"]).items()}


def test_declared_request_headers_reach_the_wire_resolved_per_request() -> None:
    """A provider can declare headers its gateway requires on every request.

    They travel as the SDK's ``extra_headers`` request option -- so the JSON body
    stays clean -- and a ``{session_id}`` value is resolved from the request
    rather than frozen when the client was built.
    """
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = dict(http_request.headers)
        seen["body"] = json.loads(http_request.content or b"{}")
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    provider = OpenAIChatCompletionsProvider(
        name="gateway",
        config=ProviderEndpointConfig(base_url="https://gateway.test/v1"),
        transport=transport,
        extra_request_headers={"x-opencode-session": "{session_id}", "x-opencode-client": "voidcode"},
    )

    provider.propose_turn(replace(_request(transport=None), provider_name="gateway", session_id="conversation-a"))
    assert _wire_headers(seen)["x-opencode-session"] == "conversation-a"
    assert _wire_headers(seen)["x-opencode-client"] == "voidcode"

    provider.propose_turn(replace(_request(transport=None), provider_name="gateway", session_id="conversation-b"))
    assert _wire_headers(seen)["x-opencode-session"] == "conversation-b"

    # No session id: the header names nothing, so it is omitted -- not sent empty.
    provider.propose_turn(replace(_request(transport=None), provider_name="gateway"))
    assert "x-opencode-session" not in _wire_headers(seen)
    assert _wire_headers(seen)["x-opencode-client"] == "voidcode"

    body = json.dumps(cast(dict[str, object], seen["body"]))
    assert "x-opencode-session" not in body
    assert "x-opencode-client" not in body


def test_gateway_request_headers_are_only_sent_by_the_provider_that_declares_them(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _sdk_over_mock_transport(seen))

    provider = OpenAIChatCompletionsProvider(name="deepseek", config=ProviderEndpointConfig(base_url="https://api.deepseek.com"))
    provider.propose_turn(replace(_request(transport=None), provider_name="deepseek", model_name="deepseek-chat", session_id="conversation-a"))

    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    headers = _wire_headers(seen)
    assert "x-opencode-session" not in headers
    assert "x-opencode-client" not in headers


def test_openai_provider_ignores_ambient_openai_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only resolved provider config supplies credentials.

    An ambient ``OPENAI_API_KEY`` used to be attached to every OpenAI-compatible
    provider, including third-party vendors and the default base URL, leaking the key.
    ``provider_configs_from_env`` still resolves it for ``providers.openai``.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-sentinel")
    seen: dict[str, object] = {}
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _sdk_over_mock_transport(seen))

    third_party = OpenAIChatCompletionsProvider(name="deepseek", config=ProviderEndpointConfig(base_url="https://api.deepseek.com"))
    third_party.propose_turn(_request(transport=None))

    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert "authorization" not in _wire_headers(seen)

    # The openai provider block resolved from the same environment still authenticates.
    configs = provider_configs_from_env({"OPENAI_API_KEY": "sk-ambient-sentinel"})
    openai_provider = OpenAIChatCompletionsProvider(name="openai", config=configs.openai)
    openai_provider.propose_turn(_request(transport=None))

    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert _wire_headers(seen)["authorization"] == "Bearer sk-ambient-sentinel"


def test_vendor_provider_without_a_config_block_uses_its_own_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent config block must never send a turn to another vendor's host.

    Every OpenAI-compatible vendor used to fall back to ``api.openai.com`` when
    its config block was absent, which sent the request -- and the vendor's
    model name -- to a host that does not serve it.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _sdk_over_mock_transport(seen))

    provider = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs()).resolve("deepseek").turn_provider()
    provider.propose_turn(replace(_request(transport=None), provider_name="deepseek", model_name="deepseek-chat"))

    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert "authorization" not in _wire_headers(seen)


def test_vendor_provider_config_block_without_base_url_keeps_its_vendor_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A block that names no base URL still resolves to the vendor's own host."""
    seen: dict[str, object] = {}
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _sdk_over_mock_transport(seen))

    registry = ModelProviderRegistry.with_defaults(provider_configs=ProviderConfigs(deepseek=OpenAICompatibleProviderConfig(api_key="sk-deepseek")))
    registry.resolve("deepseek").turn_provider().propose_turn(replace(_request(transport=None), provider_name="deepseek", model_name="deepseek-chat"))

    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert _wire_headers(seen)["authorization"] == "Bearer sk-deepseek"


def test_vendor_provider_configured_from_the_environment_uses_its_own_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential environments alone configure the provider without changing its host."""
    seen: dict[str, object] = {}
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _sdk_over_mock_transport(seen))

    registry = ModelProviderRegistry.with_defaults(provider_configs=provider_configs_from_env({"DEEPSEEK_API_KEY": "sk-deepseek-env"}))
    registry.resolve("deepseek").turn_provider().propose_turn(replace(_request(transport=None), provider_name="deepseek", model_name="deepseek-chat"))

    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert _wire_headers(seen)["authorization"] == "Bearer sk-deepseek-env"


def test_provider_without_an_endpoint_fails_typed_instead_of_borrowing_a_host() -> None:
    provider = OpenAIChatCompletionsProvider(name="acme-gateway")

    with pytest.raises(ProviderExecutionError) as raised:
        provider.propose_turn(replace(_request(transport=None), provider_name="acme-gateway"))

    assert raised.value.kind == "not_configured"
    assert raised.value.retryable is False
    assert raised.value.fallback_allowed is True
    assert "acme-gateway" in raised.value.message
    assert "providers.acme-gateway.base_url" in raised.value.message
    assert "base_url" in guidance_for_provider_error_kind("not_configured")


def test_not_configured_check_is_skipped_when_a_transport_is_injected() -> None:
    """An injected transport owns its own endpoint, so no config resolution happens."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    provider = OpenAIChatCompletionsProvider(name="acme-gateway", transport=transport)

    result = provider.propose_turn(replace(_request(transport=None), provider_name="acme-gateway"))

    assert result.output == "ok"


def test_native_stream_eof_without_finish_reason_yields_terminal_unknown() -> None:
    class _Transport:
        def request(self, _payload: dict[str, object], *, timeout_seconds: float) -> object:
            return iter(({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},))

    events = list(OpenAIModelProvider(transport=cast(object, _Transport())).turn_provider().stream_turn(_request(transport=None)))

    # The stream ended without a recognized finish reason. The adapter reports the
    # canonical terminal reason; the graph treats it as a completed turn.
    assert [(event.kind, event.channel) for event in events] == [("delta", "text"), ("done", "text")]
    assert events[-1].done_reason == "unknown"


def test_native_stream_first_event_timeout_is_typed_and_closes_stream() -> None:
    class _ClosableStream:
        def __init__(self) -> None:
            self.closed = False

        def __iter__(self) -> _ClosableStream:
            return self

        def __next__(self) -> dict[str, object]:
            import time

            time.sleep(0.05)
            if self.closed:
                raise StopIteration
            return {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}

        def close(self) -> None:
            self.closed = True

    class _Transport:
        def __init__(self) -> None:
            self.stream = _ClosableStream()

        def request(self, _payload: dict[str, object], *, timeout_seconds: float) -> object:
            return self.stream

    transport = _Transport()
    provider = OpenAIModelProvider(config=OpenAIProviderConfig(timeout_seconds=0.01), transport=cast(object, transport)).turn_provider()
    with pytest.raises(ProviderExecutionError, match="chunk timeout") as raised:
        list(provider.stream_turn(_request(transport=None)))
    assert raised.value.kind == "transient_failure"
    assert transport.stream.closed


def test_native_usage_never_reports_negative_uncached_tokens() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 4}},
            },
        )

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    result = OpenAIModelProvider(transport=transport).turn_provider().propose_turn(_request(transport=transport))
    assert result.usage is not None and result.usage.uncached_input_tokens == 0


def _tool_turn_request(*, provider_name: str, model_name: str, replayed: bool) -> ProviderTurnRequest:
    segments = (
        ProviderContextSegment(role="user", content="read it"),
        ProviderContextSegment(
            role="assistant",
            content=None,
            tool_name="read",
            tool_call_id="call-1",
            tool_arguments={"path": "a.txt"},
        ),
        ProviderContextSegment(
            role="tool",
            content="file body",
            tool_name="read",
            tool_call_id="call-1",
            metadata={
                "status": "ok",
                "data": {"path": "a.txt", "reasoning_content": "thinking trace"},
                **({"source": "replayed_conversation"} if replayed else {}),
            },
        ),
    )
    result = ToolResult(
        tool_name="read",
        status="ok",
        content="file body",
        data={"path": "a.txt", "arguments": {"path": "a.txt"}, "reasoning_content": "thinking trace"},
        source="replayed_conversation" if replayed else None,
    )
    context = _Context(prompt="read it", segments=segments, metadata={}, tool_results=(result,))
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt="read it")),
        available_tools=(),
        provider_name=provider_name,
        model_name=model_name,
        raw_model=f"{provider_name}/{model_name}",
    )


def _captured_messages(
    *,
    provider_name: str,
    config: ProviderEndpointConfig,
    request: ProviderTurnRequest,
    tool_feedback_model_overrides: dict[str, Any] | None = None,
) -> list[dict[str, object]]:
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["payload"] = json.loads(http_request.content)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    provider = OpenAIChatCompletionsProvider(
        name=provider_name,
        config=config,
        transport=transport,
        tool_feedback_model_overrides=cast(Any, tool_feedback_model_overrides or {}),
    )
    provider.propose_turn(request)
    payload = cast(dict[str, object], seen["payload"])
    return cast(list[dict[str, object]], payload["messages"])


def test_native_synthetic_tool_feedback_replaces_tool_role() -> None:
    messages = _captured_messages(
        provider_name="opencode-go",
        config=ProviderEndpointConfig(base_url="https://gateway.test/v1"),
        request=_tool_turn_request(provider_name="opencode-go", model_name="qwen3.6-plus", replayed=False),
        tool_feedback_model_overrides={"qwen3.6-plus": "synthetic_user_message"},
    )

    assert all(message["role"] != "tool" for message in messages)
    assert all("tool_calls" not in message for message in messages)
    feedback = cast(str, messages[-1]["content"])
    assert feedback.startswith("Completed tool calls for current request:")
    assert '"path": "a.txt"' in feedback


def test_native_synthetic_tool_feedback_excludes_replayed_results() -> None:
    messages = _captured_messages(
        provider_name="opencode-go",
        config=ProviderEndpointConfig(base_url="https://gateway.test/v1"),
        request=_tool_turn_request(provider_name="opencode-go", model_name="qwen3.6-plus", replayed=True),
        tool_feedback_model_overrides={"qwen3.6-plus": "synthetic_user_message"},
    )

    assert messages[-1]["content"] == "[Previous run tool result for read]\nfile body"
    assert all("Completed tool calls for current request" not in str(message["content"]) for message in messages)


def test_native_deepseek_replays_reasoning_content_on_tool_call_turns() -> None:
    messages = _captured_messages(
        provider_name="deepseek",
        config=ProviderEndpointConfig(base_url="https://api.deepseek.com"),
        request=_tool_turn_request(provider_name="deepseek", model_name="deepseek-v4-flash", replayed=False),
    )

    assistant = next(message for message in messages if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "thinking trace"


def test_native_deepseek_reasoning_replay_follows_model_map_alias() -> None:
    """An aliased deepseek model must still get its replayed reasoning_content.

    `model_map` is what actually reaches the provider, so the replay predicate has to
    judge the mapped name -- DeepSeek rejects a replayed assistant tool-call turn that
    omits it.
    """
    request = _tool_turn_request(provider_name="endpoint", model_name="ds-alias", replayed=False)
    config = ProviderEndpointConfig(base_url="https://gateway.test/v1", model_map={"ds-alias": "deepseek-chat"})
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["payload"] = json.loads(http_request.content)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    OpenAIChatCompletionsProvider(name="endpoint", config=config, transport=transport).propose_turn(request)

    payload = cast(dict[str, object], seen["payload"])
    assert payload["model"] == "deepseek-chat"
    messages = cast(list[dict[str, object]], payload["messages"])
    assistant = next(message for message in messages if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "thinking trace"


def test_native_standard_feedback_keeps_tool_role_without_reasoning_replay() -> None:
    messages = _captured_messages(
        provider_name="openai",
        config=ProviderEndpointConfig(base_url="https://api.openai.com/v1"),
        request=_tool_turn_request(provider_name="openai", model_name="gpt-4o", replayed=False),
    )

    assert [message["role"] for message in messages] == ["user", "assistant", "tool"]
    assert "reasoning_content" not in messages[1]


def _reasoning_request(
    *,
    provider_name: str,
    model_name: str,
    reasoning_effort: str,
    model_metadata: ProviderModelMetadata | None = None,
) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ProviderContextSegment(role="user", content="hello"),), metadata={})
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt="hello")),
        available_tools=(),
        provider_name=provider_name,
        model_name=model_name,
        raw_model=f"{provider_name}/{model_name}",
        reasoning_effort=reasoning_effort,
        model_metadata=model_metadata,
    )


def _captured_reasoning_body(
    *,
    provider_name: str,
    model_name: str,
    base_url: str,
    reasoning_effort: str,
    model_metadata: ProviderModelMetadata | None = None,
) -> dict[str, object]:
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["payload"] = json.loads(http_request.content)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    provider = OpenAIChatCompletionsProvider(
        name=provider_name,
        config=ProviderEndpointConfig(base_url=base_url),
        transport=transport,
    )
    provider.propose_turn(
        _reasoning_request(
            provider_name=provider_name,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            model_metadata=model_metadata,
        )
    )
    return cast(dict[str, object], seen["payload"])


@pytest.mark.parametrize(
    ("provider_name", "model_name", "base_url", "reasoning_effort", "expected"),
    [
        ("zai", "glm-5", "https://api.z.ai/api/paas/v4", "high", {"thinking": {"type": "enabled"}}),
        ("zhipuai", "glm-z1-air", "https://open.bigmodel.cn/api/paas/v4", "high", {"thinking": {"type": "enabled"}}),
        ("deepseek", "deepseek-reasoner", "https://api.deepseek.com", "off", {"thinking": {"type": "disabled"}}),
        ("deepseek", "deepseek-reasoner", "https://api.deepseek.com", "high", {"reasoning_effort": "high"}),
        (
            "opencode-go",
            "minimax-m2.7",
            "https://opencode.ai/zen/go",
            "high",
            {"reasoning_effort": "high"},
        ),
    ],
)
def test_native_reasoning_body_fields_reach_the_wire(
    provider_name: str,
    model_name: str,
    base_url: str,
    reasoning_effort: str,
    expected: dict[str, object],
) -> None:
    body = _captured_reasoning_body(
        provider_name=provider_name,
        model_name=model_name,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
    )

    assert body["model"] == model_name
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    for key, value in expected.items():
        assert body[key] == value
    # `extra_body` is an SDK-level envelope the client merges into the JSON body;
    # it must never appear on the wire itself.
    assert "extra_body" not in body


def test_native_clamps_reasoning_effort_to_the_models_shipped_levels() -> None:
    # Shipped metadata for deepseek-v4-pro is low/high/max. The clamp (not a
    # provider-name table) now decides the level, so "medium" snaps down to "low";
    # the removed hardcoded DeepSeek table sent "high" here.
    metadata = static_catalog_metadata("deepseek", "deepseek-v4-pro")
    assert metadata is not None
    assert metadata.supported_effort_levels == ("low", "high", "max")

    body = _captured_reasoning_body(
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        reasoning_effort="medium",
        model_metadata=metadata,
    )

    assert body["reasoning_effort"] == "low"
    assert "thinking" not in body


def test_native_sends_the_lowest_supported_level_for_off() -> None:
    # New "off" rule: never a bare "none"; the model gets the least reasoning it
    # supports, and a model without levels gets no effort field at all.
    metadata = static_catalog_metadata("openai", "gpt-5.5")
    assert metadata is not None

    body = _captured_reasoning_body(
        provider_name="openai",
        model_name="gpt-5.5",
        base_url="https://api.openai.com/v1",
        reasoning_effort="off",
        model_metadata=metadata,
    )

    assert body["reasoning_effort"] == "low"
    assert "thinking" not in body

    omitted = _captured_reasoning_body(
        provider_name="groq",
        model_name="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        reasoning_effort="off",
        model_metadata=ProviderModelMetadata(context_window=131_072),
    )

    assert "reasoning_effort" not in omitted

    # opencode-go/minimax-m2.7 keeps the exact values its removed name-keyed ladder
    # produced: "off" still lands on the lowest supported level.
    minimax = static_catalog_metadata("opencode-go", "minimax-m2.7")
    assert minimax is not None
    minimax_body = _captured_reasoning_body(
        provider_name="opencode-go",
        model_name="minimax-m2.7",
        base_url="https://opencode.ai/zen/go",
        reasoning_effort="off",
        model_metadata=minimax,
    )

    assert minimax_body["reasoning_effort"] == "low"


def test_native_gateway_model_levels_reach_the_wire_from_the_shipped_catalog() -> None:
    # opencode-go/deepseek-v4.1-flash used to be rejected outright (the gateway had
    # a two-name provider allowlist and the model was not catalogued). Its shipped
    # levels are low/high/max, so medium (and minimal/off) snap down to low and the
    # catalogue's own levels pass through untouched.
    metadata = static_catalog_metadata("opencode-go", "deepseek-v4.1-flash")
    assert metadata is not None
    assert metadata.supports_reasoning_effort is True
    assert metadata.supported_effort_levels == ("low", "high", "max")

    for effort, expected in (("off", "low"), ("low", "low"), ("medium", "low"), ("high", "high"), ("max", "max")):
        body = _captured_reasoning_body(
            provider_name="opencode-go",
            model_name="deepseek-v4.1-flash",
            base_url="https://opencode.ai/zen/go",
            reasoning_effort=effort,
            model_metadata=metadata,
        )
        assert body["reasoning_effort"] == expected, effort


def test_native_sanitizes_mcp_tool_names_and_decodes_runtime_names() -> None:
    definition = ToolDefinition(
        name="mcp/grep_app/searchGitHub",
        description="search github code",
        input_schema={"query": {"type": "string"}},
    )
    context = _Context(
        prompt="search",
        segments=(ProviderContextSegment(role="user", content="search"),),
        metadata={},
    )
    request = ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt="search")),
        available_tools=(definition,),
        provider_name="deepseek",
        model_name="deepseek-v4-pro",
        raw_model="deepseek/deepseek-v4-pro",
    )
    provider = OpenAIChatCompletionsProvider(name="deepseek", config=ProviderEndpointConfig(base_url="https://api.deepseek.com"))

    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(http_request.content)
        seen["payload"] = payload
        wire_name = _wire_tool_names(payload)[0]
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [{"id": "call-1", "function": {"name": wire_name, "arguments": json.dumps({"query": "triangle"})}}],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    result = OpenAIChatCompletionsProvider(name="deepseek", config=provider.config, transport=transport).propose_turn(request)

    payload = cast(dict[str, object], seen["payload"])
    wire_name = _wire_tool_names(payload)[0]
    # The provider-side tool name must be a legal function name: no path separators.
    assert "/" not in wire_name
    assert result.tool_call is not None
    assert result.tool_call.tool_name == "mcp/grep_app/searchGitHub"
    assert result.tool_call.arguments == {"query": "triangle"}


def test_native_rejects_cached_turns_without_anthropic_messages() -> None:
    """The Chat Completions wire cannot carry prompt caching, so a cached turn must fail.

    `cache_retention` is not exposed by any schema that reaches this adapter (only
    `providers.anthropic` parses it), so the reachable trigger is the protocol-level
    `ProviderTurnRequest.cache_retention` field; the config-level read is defensive.
    """
    config = ProviderEndpointConfig(base_url="https://api.openai.com/v1")
    provider = OpenAIChatCompletionsProvider(name="openai", config=config)

    with pytest.raises(ProviderExecutionError, match="Anthropic Messages-compatible") as raised:
        provider.propose_turn(replace(_request(transport=None), cache_retention="short"))

    assert raised.value.kind == "unsupported_feature"
    assert raised.value.retryable is False
    # Another provider in the chain may be Anthropic-compatible, so a fallback is allowed.
    assert raised.value.fallback_allowed is True

    # A turn without cache retention is served normally.
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    result = OpenAIChatCompletionsProvider(name="openai", config=config, transport=transport).propose_turn(_request(transport=transport))
    assert result.output == "ok"


def test_native_stream_keeps_parallel_tool_call_fragments_isolated() -> None:
    body = "\n\n".join(
        [
            'data: {"choices":[{"delta":{"tool_calls":['
            '{"index":0,"id":"call-a","function":{"name":"read","arguments":"{\\"path\\":"}},'
            '{"index":1,"id":"call-b","function":{"name":"read","arguments":"{\\"path\\":"}}'
            ']},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":['
            '{"index":1,"function":{"arguments":"\\"b.txt\\"}"}},'
            '{"index":0,"function":{"arguments":"\\"a.txt\\"}"}}'
            ']},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
            "",
        ]
    )

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    events = list(OpenAIModelProvider(transport=transport).turn_provider().stream_turn(_request(transport=transport)))

    ends = {event.tool_call_id: event.parsed_arguments for event in events if event.kind == "tool_call_end"}
    assert ends == {"call-a": {"path": "a.txt"}, "call-b": {"path": "b.txt"}}


def _stream_events(body: str) -> list[ProviderStreamEvent]:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return list(OpenAIModelProvider(transport=transport).turn_provider().stream_turn(_request(transport=transport)))


@pytest.mark.parametrize("event", ["{not json}", "[1, 2]", '"a string"'])
def test_native_undecodable_stream_events_are_typed_and_bounded(event: str) -> None:
    with pytest.raises(ProviderExecutionError) as raised:
        _stream_events(f"data: {event}\n\ndata: [DONE]\n\n")

    error = raised.value
    assert error.kind == "transient_failure"
    assert error.retryable is True
    assert error.fallback_allowed is True
    assert error.message == "provider stream event was not a valid JSON object"
    details = cast(dict[str, object], error.details)
    assert details["source"] == "stream"
    assert details["guidance"]
    inner = cast(dict[str, object], details["details"])
    assert inner["reason"] == "invalid_stream_event"
    assert isinstance(inner["exception_type"], str) and inner["exception_type"]
    # The decoder's own text is not forwarded into the persisted payload.
    rendered = json.dumps(details)
    assert "not json" not in rendered
    assert "Expecting" not in rendered


def test_native_stream_requires_spec_compliant_event_framing() -> None:
    """SSE events must be blank-line terminated; lenient framing loses the event.

    The SDK's decoder dispatches an event only on the blank line that ends it, so a
    stream that separates events with a single newline delivers nothing, and a final
    event without its terminating blank line is dropped. The adapter no longer raises
    for these: the turn resolves to a terminal ``unknown`` reason with no text, and
    the empty response still fails at the graph ("neither output nor tool calls").
    """
    payload = '{"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'

    for terminator in ("\n\n", "\r\n\r\n"):
        events = _stream_events(f"data: {payload}{terminator}data: [DONE]{terminator}")
        assert [(event.kind, event.channel) for event in events] == [("delta", "text"), ("done", "text")]
        assert events[-1].done_reason == "stop"

    for lenient_body in (f"data: {payload}\ndata: [DONE]\n", f"data: {payload}"):
        events = _stream_events(lenient_body)
        # No text survived the dropped event framing, so there is no partial answer
        # to report — only the terminal, unrecognized reason.
        assert [event.kind for event in events] == ["done"]
        assert events[-1].done_reason == "unknown"


def _proposed_turn(finish_reason: object, *, usage: bool = False) -> ProviderTurnResult:
    payload: dict[str, object] = {"choices": [{"message": {"content": "ok"}, "finish_reason": finish_reason}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 3, "completion_tokens": 1}

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    provider = OpenAIChatCompletionsProvider(name="deepseek", config=ProviderEndpointConfig(base_url="https://api.deepseek.com"), transport=transport)
    return provider.propose_turn(_request(transport=transport))


def test_native_unrecognized_finish_reason_keeps_the_provider_token() -> None:
    result = _proposed_turn("eos_token")

    assert result.done_reason == "unknown"
    assert result.finish_reason_reported is True
    # The graph treats an unrecognized reason as a completed turn; the provider's own
    # token stays on metadata so the resolution stays diagnosable instead of a bare
    # "unknown".
    assert cast(dict[str, object], result.metadata)["finish_reason_raw"] == "eos_token"

    # A recognized reason carries no raw token, even when usage rides along in the
    # same response (the non-stream analogue of a trailing usage-only chunk).
    recognized = _proposed_turn("stop", usage=True)
    assert recognized.done_reason == "stop"
    assert recognized.usage is not None and recognized.usage.output_tokens == 1
    assert "finish_reason_raw" not in cast(dict[str, object], recognized.metadata)


def test_native_non_stream_finish_reason_reporting_follows_the_wire() -> None:
    def propose(choice: dict[str, object]) -> ProviderTurnResult:
        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json={"choices": [choice]})

        transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        provider = OpenAIChatCompletionsProvider(
            name="deepseek", config=ProviderEndpointConfig(base_url="https://api.deepseek.com"), transport=transport
        )
        return provider.propose_turn(_request(transport=transport))

    # An absent key or an explicit null is the silent-truncation case: the turn
    # completes on the canonical "unknown" reason but was not actually reported.
    absent = propose({"message": {"content": "ok"}})
    assert (absent.done_reason, absent.finish_reason_reported) == ("unknown", False)
    null = propose({"message": {"content": "ok"}, "finish_reason": None})
    assert (null.done_reason, null.finish_reason_reported) == ("unknown", False)
    # A reported but unrecognized token is still reported.
    unrecognized = propose({"message": {"content": "ok"}, "finish_reason": "eos_token"})
    assert (unrecognized.done_reason, unrecognized.finish_reason_reported) == ("unknown", True)
    recognized = propose({"message": {"content": "ok"}, "finish_reason": "stop"})
    assert (recognized.done_reason, recognized.finish_reason_reported) == ("stop", True)


def test_native_trailing_usage_chunk_keeps_the_latched_finish_reason() -> None:
    usage_chunk = 'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4}}'

    def stream_body(reason: str) -> str:
        return "\n\n".join(
            (
                f'data: {{"choices":[{{"delta":{{"content":"hi"}},"finish_reason":"{reason}"}}]}}',
                usage_chunk,
                "data: [DONE]",
                "",
            )
        )

    stopped = _stream_events(stream_body("stop"))
    assert stopped[-1].done_reason == "stop"
    assert stopped[-1].usage is not None and stopped[-1].usage.output_tokens == 4
    # The usage-only chunk must not attach or clear a raw token.
    assert "finish_reason_raw" not in cast(dict[str, object], stopped[-1].metadata or {})

    unrecognized = _stream_events(stream_body("eos_token"))
    assert unrecognized[-1].done_reason == "unknown"
    assert cast(dict[str, object], unrecognized[-1].metadata or {})["finish_reason_raw"] == "eos_token"


def test_native_usage_metadata_payload_reports_cache_read_and_hit_rate() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 80}},
            },
        )

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    result = OpenAIModelProvider(transport=transport).turn_provider().propose_turn(_request(transport=transport))

    usage = result.usage
    assert usage is not None
    payload = usage.metadata_payload()
    assert payload["input_tokens"] == 100
    assert payload["output_tokens"] == 20
    assert payload["cache_read_tokens"] == 80
    assert payload["uncached_input_tokens"] == 20
    assert usage.cache_hit_rate == 0.8


def _wire_request(
    *,
    user_text: str,
    system_text: str | None = "Be concise.",
    tool_names: tuple[str, ...] = ("read",),
) -> ProviderTurnRequest:
    segments: list[ProviderContextSegment] = []
    if system_text is not None:
        segments.append(ProviderContextSegment(role="system", content=system_text))
    segments.append(ProviderContextSegment(role="user", content=user_text))
    context = _Context(prompt=user_text, segments=tuple(segments), metadata={})
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt=user_text)),
        available_tools=tuple(
            ToolDefinition(name=name, description=f"{name} tool", input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
            for name in tool_names
        ),
        provider_name="openai",
        model_name="gpt-4o",
        raw_model="openai/gpt-4o",
    )


def test_wire_prefix_descriptor_is_a_final_materialization_seam() -> None:
    provider = OpenAIChatCompletionsProvider(name="openai", config=ProviderEndpointConfig(base_url="https://api.openai.com/v1"))
    request = _wire_request(user_text="first")

    first = provider._wire(request)
    second = provider._wire(request)

    assert first.prefix.canonical_hash == second.prefix.canonical_hash
    assert first.prefix.tool_generation == second.prefix.tool_generation
    assert first.prefix.materialized_message_count == len(first.messages)
    assert first.prefix.materialized_message_count == 2
    assert hashlib.sha256(first.prefix.canonical_bytes).hexdigest() == first.prefix.canonical_hash

    # A non-prefix message is not part of the canonical prefix.
    changed_body = provider._wire(_wire_request(user_text="second"))
    assert changed_body.prefix.canonical_hash == first.prefix.canonical_hash
    assert changed_body.prefix.tool_generation == first.prefix.tool_generation

    # The stable system prefix is part of it.
    changed_prefix = provider._wire(_wire_request(user_text="first", system_text="Different policy."))
    assert changed_prefix.prefix.canonical_hash != first.prefix.canonical_hash
    assert changed_prefix.prefix.tool_generation == first.prefix.tool_generation

    changed_tools = provider._wire(_wire_request(user_text="first", tool_names=("read", "glob")))
    assert changed_tools.prefix.tool_generation != first.prefix.tool_generation


def test_wire_prefix_materialized_message_count_matches_emitted_payload() -> None:
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["payload"] = json.loads(http_request.content)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    request = _wire_request(user_text="first")
    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    provider = OpenAIChatCompletionsProvider(name="openai", config=ProviderEndpointConfig(base_url="https://api.openai.com/v1"), transport=transport)
    wire = provider._wire(request)
    provider.propose_turn(request)

    payload = cast(dict[str, object], seen["payload"])
    emitted = cast(list[dict[str, object]], payload["messages"])
    assert emitted == wire.messages
    assert wire.prefix.materialized_message_count == len(emitted) == 2
    assert [message["role"] for message in emitted] == ["system", "user"]


_LONG_TOOL_BASE = "mcp/" + "tool" * 20


def test_native_tool_name_cap_truncates_to_64_and_preserves_hash_suffix() -> None:
    name_a = _LONG_TOOL_BASE + "alpha"
    name_b = _LONG_TOOL_BASE + "bravo"

    wire_names = _wire_tool_names(_captured_tool_payload((name_a, name_b)))
    wire_a, wire_b = wire_names

    assert len(name_a) > 64
    assert len(wire_a) == 64
    assert len(wire_b) == 64
    assert wire_a != wire_b
    # Distinct long names keep their shared truncated head, so identity rides on the suffix.
    assert wire_a[:55] == wire_b[:55]
    assert wire_a.endswith("_" + hashlib.sha1(name_a.encode("utf-8")).hexdigest()[:8])
    assert wire_b.endswith("_" + hashlib.sha1(name_b.encode("utf-8")).hexdigest()[:8])
    # Mapping is deterministic across materializations of the same request.
    assert _wire_tool_names(_captured_tool_payload((name_a, name_b))) == wire_names


def _captured_tool_payload(tool_names: tuple[str, ...]) -> dict[str, object]:
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        seen["payload"] = json.loads(http_request.content)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    provider = OpenAIChatCompletionsProvider(name="deepseek", config=ProviderEndpointConfig(base_url="https://api.deepseek.com"), transport=transport)
    provider.propose_turn(_wire_request(user_text="search", tool_names=tool_names))
    return cast(dict[str, object], seen["payload"])


def test_native_capped_tool_name_round_trips_through_transport_payload() -> None:
    tool_name = _LONG_TOOL_BASE + "alpha"
    result = ToolResult(
        tool_name=tool_name,
        status="ok",
        content="matches",
        data={"query": "triangle", "tool_call_id": "call-1", "arguments": {"query": "triangle"}},
    )
    segments = (
        ProviderContextSegment(role="user", content="search"),
        ProviderContextSegment(role="assistant", content=None, tool_name=tool_name, tool_call_id="call-1", tool_arguments={"query": "triangle"}),
        ProviderContextSegment(
            role="tool", content=result.content, tool_name=tool_name, tool_call_id="call-1", metadata={"status": "ok", "data": result.data}
        ),
    )
    context = _Context(prompt="search", segments=segments, metadata={}, tool_results=(result,))
    request = ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        bounded_context_window=cast(ProviderContextWindow, _ContextWindow(prompt="search")),
        available_tools=(
            ToolDefinition(name=tool_name, description="search", input_schema={"type": "object", "properties": {"query": {"type": "string"}}}),
        ),
        provider_name="deepseek",
        model_name="deepseek-v4-flash",
        raw_model="deepseek/deepseek-v4-flash",
    )
    provider = OpenAIChatCompletionsProvider(name="deepseek", config=ProviderEndpointConfig(base_url="https://api.deepseek.com"))
    seen: dict[str, object] = {}

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(http_request.content)
        seen["payload"] = payload
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {"id": "call-2", "function": {"name": _wire_tool_names(payload)[0], "arguments": json.dumps({"query": "square"})}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    transport = OpenAIChatCompletionsTransport(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    turn = OpenAIChatCompletionsProvider(name="deepseek", config=provider.config, transport=transport).propose_turn(request)

    payload = cast(dict[str, object], seen["payload"])
    wire_name = _wire_tool_names(payload)[0]
    assert len(wire_name) == 64
    assert wire_name != tool_name

    messages = cast(list[dict[str, object]], payload["messages"])
    assistant = next(message for message in messages if message["role"] == "assistant")
    calls = cast(list[dict[str, object]], assistant["tool_calls"])
    assert cast(dict[str, object], calls[0]["function"])["name"] == wire_name
    tool_message = next(message for message in messages if message["role"] == "tool")
    assert f'"tool_name": "{wire_name}"' in cast(str, tool_message["content"])

    assert turn.tool_call is not None
    assert turn.tool_call.tool_name == tool_name


def _recording_sdk(observed: dict[str, object]) -> Any:
    class _Completions:
        def create(self, **payload: object) -> dict[str, object]:
            _ = payload
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class _Chat:
        completions = _Completions()

    class _SDK:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)
            self.chat = _Chat()

    return _SDK


def test_openai_transport_forwards_ssl_verify_false_to_constructed_client(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx2.Client
    client_kwargs: dict[str, object] = {}
    observed_sdk: dict[str, object] = {}

    def recording_client(*args: object, **kwargs: object) -> httpx2.Client:
        client_kwargs.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "Client", recording_client)
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _recording_sdk(observed_sdk))

    transport = OpenAIChatCompletionsTransport(base_url="https://gateway.test/v1", ssl_verify=False)
    transport.request({"model": "gpt-4o"}, timeout_seconds=1)

    assert client_kwargs == {"verify": False}
    assert isinstance(observed_sdk["http_client"], real_client)


def test_openai_transport_keeps_default_verification_when_ssl_verify_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_sdk: dict[str, object] = {}

    def forbidden_client(*args: object, **kwargs: object) -> httpx2.Client:
        _ = args, kwargs
        raise AssertionError("transport must not build a custom HTTP client when ssl_verify is unset")

    monkeypatch.setattr(httpx2, "Client", forbidden_client)
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _recording_sdk(observed_sdk))

    transport = OpenAIChatCompletionsTransport(base_url="https://gateway.test/v1")
    transport.request({"model": "gpt-4o"}, timeout_seconds=1)

    assert observed_sdk["http_client"] is None


def test_openai_transport_disables_sdk_internal_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_sdk: dict[str, object] = {}
    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _recording_sdk(observed_sdk))

    transport = OpenAIChatCompletionsTransport(base_url="https://gateway.test/v1", api_key="sk-test")
    transport.request({"model": "gpt-4o"}, timeout_seconds=1)

    # Runtime owns retry and fallback: the SDK must not retry underneath it.
    assert observed_sdk["max_retries"] == 0


def test_provider_builds_one_sdk_client_for_many_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, object]] = []

    class _Completions:
        def create(self, **payload: object) -> dict[str, object]:
            _ = payload
            return {"id": "chatcmpl-1", "model": "gpt-4o", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    class _Chat:
        completions = _Completions()

    class _SDK:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs)
            self.chat = _Chat()

    monkeypatch.setattr("voidcode.provider.openai_native.OpenAI", _SDK)
    provider = OpenAIChatCompletionsProvider(config=OpenAIProviderConfig(base_url="https://gateway.test/v1", api_key="sk-test"))

    outputs = [provider.propose_turn(_request(transport=None)).output for _ in range(3)]

    assert outputs == ["ok", "ok", "ok"]
    assert len(built) == 1
