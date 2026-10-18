"""Unit tests for the openai-chat provider plugin using real loopback HTTP."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.provider_loopback import OpenAILoopbackServer
from voidcode.plugins.openai_chat.openai_plugin import OpenAIChatProvider
from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderMessage,
    ProviderTurnRequest,
)
from voidcode.sdk.tools import ToolDefinition, ToolEffect


class DummyAbortSignal:
    def __init__(self, cancelled: bool = False, reason: str | None = None) -> None:
        self._cancelled = cancelled
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        self._reason = reason


@pytest.fixture
def loopback_server():
    server = OpenAILoopbackServer([])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_openai_provider_unconfigured_and_missing_credentials() -> None:
    # Missing provider_name in request
    provider = OpenAIChatProvider({})
    req0 = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="hello"),),
        provider_name=None,
        model_name="deepseek-flash",
    )
    with pytest.raises(ProviderExecutionError) as exc_info0:
        provider.propose_turn(req0)
    assert exc_info0.value.kind == "not_configured"
    assert "request.provider_name is required" in exc_info0.value.message

    # Unknown provider
    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="hello"),),
        provider_name="unknown-provider",
        model_name="deepseek-flash",
    )
    with pytest.raises(ProviderExecutionError) as exc_info:
        provider.propose_turn(req)
    assert exc_info.value.kind == "not_configured"

    # Provider exists but missing api_key (e.g. None or empty string)
    provider2 = OpenAIChatProvider({"providers": {"deepseek": {"api_key": None, "base_url": "http://127.0.0.1:8080/v1", "wire": "openai-chat"}}})
    req2 = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="hello"),),
        provider_name="deepseek",
        model_name="deepseek-flash",
    )
    with pytest.raises(ProviderExecutionError) as exc_info2:
        provider2.propose_turn(req2)
    assert exc_info2.value.kind == "not_configured"
    assert "missing credential" in exc_info2.value.message

    # Provider exists but missing base_url
    provider3 = OpenAIChatProvider({"providers": {"deepseek": {"api_key": "key", "wire": "openai-chat"}}})
    with pytest.raises(ProviderExecutionError) as exc_info3:
        provider3.propose_turn(req2)
    assert exc_info3.value.kind == "not_configured"
    assert "explicit absolute HTTP(S) base_url" in exc_info3.value.message


def test_openai_provider_missing_model_name() -> None:
    provider = OpenAIChatProvider({"providers": {"openai": {"api_key": "key", "base_url": "http://127.0.0.1:8080/v1", "wire": "openai-chat"}}})
    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="hello"),),
        provider_name="openai",
        model_name=None,
    )
    with pytest.raises(ProviderExecutionError) as exc_info:
        provider.propose_turn(req)
    assert exc_info.value.kind == "invalid_model"


def test_openai_provider_honors_cancellation_before_request() -> None:
    provider = OpenAIChatProvider({"providers": {"openai": {"api_key": "key", "base_url": "http://127.0.0.1:8080/v1", "wire": "openai-chat"}}})
    abort = DummyAbortSignal(cancelled=True)
    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="hello"),),
        provider_name="openai",
        model_name="deepseek-flash",
        abort_signal=abort,
    )
    with pytest.raises(ProviderExecutionError) as exc_info:
        provider.propose_turn(req)
    assert exc_info.value.kind == "cancelled"


def test_openai_provider_success_turn_and_no_tool_choice_injection(loopback_server: OpenAILoopbackServer) -> None:
    port = loopback_server.server_port
    base_url = f"http://127.0.0.1:{port}/v1"

    loopback_server.replies.append(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Hello world response",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 5,
            },
        }
    )

    provider = OpenAIChatProvider(
        {
            "providers": {
                "deepseek": {
                    "api_key": "test-key",
                    "base_url": base_url,
                    "wire": "openai-chat",
                }
            }
        }
    )

    tool_def = ToolDefinition("read", "Read file", input_schema={"type": "object"}, effects=frozenset({ToolEffect.READ}))
    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="Say hello"),),
        available_tools=(tool_def,),
        provider_name="deepseek",
        model_name="deepseek-flash",
    )

    result = provider.propose_turn(req)
    provider.close()

    assert result.output == "Hello world response"
    assert result.done_reason == "stop"
    assert result.finish_reason_reported is True
    assert result.usage is not None
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 5

    # Check request that reached wire
    assert len(loopback_server.requests) == 1
    wire_req = loopback_server.requests[0]
    assert wire_req["model"] == "deepseek-flash"
    assert wire_req["messages"] == [{"role": "user", "content": "Say hello"}]
    assert "tool_choice" not in wire_req  # Never injected
    assert len(wire_req["tools"]) == 1
    assert wire_req["tools"][0]["function"]["name"] == "read"


def test_openai_provider_tool_calls_sequence_and_strict_validation(loopback_server: OpenAILoopbackServer) -> None:
    port = loopback_server.server_port
    base_url = f"http://127.0.0.1:{port}/v1"

    loopback_server.replies.append(
        {
            "id": "chatcmpl-tc",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
                            },
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "write", "arguments": '{"path":"b.txt","content":"data"}'},
                            },
                        ],
                    },
                }
            ],
        }
    )

    provider = OpenAIChatProvider(
        {
            "providers": {
                "openai": {
                    "api_key": "test-key",
                    "base_url": base_url,
                    "wire": "openai-chat",
                }
            }
        }
    )

    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="run tools"),),
        provider_name="openai",
        model_name="deepseek-flash",
    )

    result = provider.propose_turn(req)
    provider.close()

    assert result.done_reason == "tool_calls"
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].tool_call_id == "call-1"
    assert result.tool_calls[0].tool_name == "read"
    assert result.tool_calls[0].arguments == {"path": "a.txt"}
    assert result.tool_calls[1].tool_call_id == "call-2"
    assert result.tool_calls[1].tool_name == "write"
    assert result.tool_calls[1].arguments == {"path": "b.txt", "content": "data"}


def test_openai_provider_native_reasoning_and_true_replay(loopback_server: OpenAILoopbackServer) -> None:
    port = loopback_server.server_port
    base_url = f"http://127.0.0.1:{port}/v1"

    # Turn 1 reply with reasoning
    loopback_server.replies.append(
        {
            "id": "chatcmpl-r1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "Detailed reasoning here.",
                        "tool_calls": [
                            {
                                "id": "call-read",
                                "type": "function",
                                "function": {"name": "read", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
        }
    )

    # Turn 2 reply
    loopback_server.replies.append(
        {
            "id": "chatcmpl-r2",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "Done with tool",
                    },
                }
            ],
        }
    )

    provider = OpenAIChatProvider(
        {
            "providers": {
                "openai": {
                    "api_key": "test-key",
                    "base_url": base_url,
                    "wire": "openai-chat",
                }
            }
        }
    )

    # Turn 1
    req1 = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="read something"),),
        provider_name="openai",
        model_name="deepseek-v4-pro",
    )
    res1 = provider.propose_turn(req1)
    assert res1.reasoning == "Detailed reasoning here."

    # Turn 2: replay assistant with native reasoning
    assistant_msg = ProviderMessage(
        role="assistant",
        content=None,
        reasoning=res1.reasoning,
        tool_calls=res1.tool_calls,
    )
    tool_msg = ProviderMessage(
        role="tool",
        tool_call_id="call-read",
        content="file contents",
    )
    req2 = ProviderTurnRequest(
        messages=(
            ProviderMessage(role="user", content="read something"),
            assistant_msg,
            tool_msg,
        ),
        provider_name="openai",
        model_name="deepseek-v4-pro",
    )
    res2 = provider.propose_turn(req2)
    provider.close()

    assert res2.output == "Done with tool"

    # Verify wire replay payload
    wire_req2 = loopback_server.requests[1]
    replayed_asst = wire_req2["messages"][1]
    assert replayed_asst["role"] == "assistant"
    assert replayed_asst["reasoning_content"] == "Detailed reasoning here."
    assert replayed_asst["tool_calls"][0]["id"] == "call-read"


def test_openai_provider_usage_none_vs_zero_preservation(loopback_server: OpenAILoopbackServer) -> None:
    port = loopback_server.server_port
    base_url = f"http://127.0.0.1:{port}/v1"

    # Reply 1: usage completely omitted
    loopback_server.replies.append(
        {
            "id": "chatcmpl-u1",
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
        }
    )

    # Reply 2: usage has explicit zeros
    loopback_server.replies.append(
        {
            "id": "chatcmpl-u2",
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }
    )

    provider = OpenAIChatProvider({"providers": {"openai": {"api_key": "k", "base_url": base_url, "wire": "openai-chat"}}})

    # Turn 1
    res1 = provider.propose_turn(ProviderTurnRequest(messages=(), provider_name="openai", model_name="m"))
    assert res1.usage is None

    # Turn 2
    res2 = provider.propose_turn(ProviderTurnRequest(messages=(), provider_name="openai", model_name="m"))
    assert res2.usage is not None
    assert res2.usage.input_tokens == 0
    assert res2.usage.output_tokens == 0
    assert res2.usage.cache_read_tokens == 0
    provider.close()


def test_openai_provider_rejects_duplicate_call_ids_and_malformed_arguments(
    loopback_server: OpenAILoopbackServer,
) -> None:
    port = loopback_server.server_port
    base_url = f"http://127.0.0.1:{port}/v1"

    # Duplicate call ID
    loopback_server.replies.append(
        {
            "id": "chatcmpl-dup",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "same-id", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                            {"id": "same-id", "type": "function", "function": {"name": "g", "arguments": "{}"}},
                        ],
                    },
                }
            ],
        }
    )

    provider = OpenAIChatProvider({"providers": {"openai": {"api_key": "k", "base_url": base_url, "wire": "openai-chat"}}})

    with pytest.raises(ProviderExecutionError) as exc_info:
        provider.propose_turn(ProviderTurnRequest(messages=(), provider_name="openai", model_name="m"))
    assert "Duplicate tool call id: same-id" in exc_info.value.message
    assert exc_info.value.kind == "provider_error"

    # Malformed arguments JSON
    loopback_server.replies.append(
        {
            "id": "chatcmpl-badargs",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{not-valid-json"}},
                        ],
                    },
                }
            ],
        }
    )

    with pytest.raises(ProviderExecutionError) as exc_info2:
        provider.propose_turn(ProviderTurnRequest(messages=(), provider_name="openai", model_name="m"))
    assert "Malformed tool call arguments JSON" in exc_info2.value.message
    provider.close()


def test_openai_provider_content_filter_and_refusal(loopback_server: OpenAILoopbackServer) -> None:
    port = loopback_server.server_port
    base_url = f"http://127.0.0.1:{port}/v1"

    # Refusal in message
    loopback_server.replies.append(
        {
            "id": "chatcmpl-ref",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "I cannot fulfill this request.",
                        "refusal": "Refused due to safety policy.",
                    },
                }
            ],
        }
    )

    provider = OpenAIChatProvider({"providers": {"openai": {"api_key": "k", "base_url": base_url, "wire": "openai-chat"}}})

    res = provider.propose_turn(ProviderTurnRequest(messages=(), provider_name="openai", model_name="m"))
    provider.close()

    assert res.done_reason == "content_filter"
    assert res.metadata is not None
    assert res.metadata["refusal"] == "Refused due to safety policy."


@pytest.mark.parametrize(("finish", "expected"), [(None, "unknown"), ("length", "length"), ("unrecognized", "unknown")])
def test_provider_preserves_noncompletion_and_cache_evidence(loopback_server: OpenAILoopbackServer, finish: str | None, expected: str) -> None:
    loopback_server.replies.append(
        {
            "id": "edge",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "incomplete"}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10},
        }
    )
    provider = OpenAIChatProvider({"providers": {"edge": {"api_key": "key", "base_url": f"http://127.0.0.1:{loopback_server.server_port}/v1"}}})
    try:
        result = provider.propose_turn(ProviderTurnRequest(messages=(ProviderMessage("user", "edge"),), provider_name="edge", model_name="m"))
        assert result.done_reason == expected
        assert result.finish_reason_reported is (finish is not None)
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 0
        assert result.usage.cache_read_tokens == 0
        assert result.usage.uncached_input_tokens == 10
    finally:
        provider.close()


def test_provider_http_error_never_exposes_response_body_or_credentials() -> None:
    secret = "upstream-secret-must-not-escape"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps({"error": {"message": secret, "code": secret, "type": "invalid_request_error"}}).encode()
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = OpenAIChatProvider({"providers": {"edge": {"api_key": secret, "base_url": f"http://127.0.0.1:{server.server_port}/v1"}}})
    try:
        with pytest.raises(ProviderExecutionError) as captured:
            provider.propose_turn(ProviderTurnRequest(messages=(ProviderMessage("user", "error"),), provider_name="edge", model_name="m"))
        assert captured.value.kind == "provider_error"
        assert captured.value.details["status_code"] == 400
        assert secret not in str(captured.value)
        assert secret not in json.dumps(captured.value.details)
    finally:
        provider.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
