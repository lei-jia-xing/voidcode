from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest
from google import genai
from google.genai import types
from google.oauth2 import service_account

from voidcode.provider.config import GoogleProviderAuthConfig, GoogleProviderConfig
from voidcode.provider.google_native import GoogleGenAIProvider
from voidcode.provider.model_catalog import ProviderModelMetadata
from voidcode.provider.protocol import (
    ProviderAssembledContext,
    ProviderContextSegment,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTurnRequest,
)
from voidcode.tools.contracts import ToolCall


@dataclass(frozen=True)
class _Context:
    prompt: str
    segments: tuple[ProviderContextSegment, ...]
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _FunctionCall:
    name: str
    args: dict[str, object]


@dataclass(frozen=True)
class _Part:
    function_call: _FunctionCall | None = None
    text: str | None = None
    thought: bool = False


@dataclass(frozen=True)
class _Content:
    parts: list[_Part]


@dataclass(frozen=True)
class _Candidate:
    content: _Content
    finish_reason: object = "STOP"


@dataclass(frozen=True)
class _Response:
    candidates: list[_Candidate]
    text: str = ""


class _FakeModels:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.non_stream_calls = 0
        self.stream_calls = 0
        self.last_config: Any | None = None

    def generate_content(self, **kwargs: object) -> _Response:
        self.non_stream_calls += 1
        self.last_config = kwargs.get("config")
        return self.response

    def generate_content_stream(self, **kwargs: object) -> Any:
        self.stream_calls += 1
        self.last_config = kwargs.get("config")
        return iter([self.response])


class _FakeClient:
    def __init__(self, response: _Response) -> None:
        self.models = _FakeModels(response)


def _request(
    *,
    tools: tuple[object, ...] = (),
    model_name: str = "gemini-2.5-flash",
    reasoning_effort: str | None = None,
    model_metadata: ProviderModelMetadata | None = None,
    session_id: str | None = None,
) -> ProviderTurnRequest:
    context = _Context(prompt="hello", segments=(ProviderContextSegment(role="user", content="hello"),))
    return ProviderTurnRequest(
        assembled_context=cast(ProviderAssembledContext, context),
        available_tools=cast(Any, tools),
        provider_name="google",
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        model_metadata=model_metadata,
        session_id=session_id,
    )


def _response_with_repeated_function_names() -> _Response:
    return _Response(
        candidates=[
            _Candidate(
                content=_Content(
                    parts=[
                        _Part(function_call=_FunctionCall(name="read", args={"path": "a.txt"})),
                        _Part(text="calling read twice"),
                        _Part(function_call=_FunctionCall(name="read", args={"path": "b.txt"})),
                        _Part(function_call=_FunctionCall(name="grep", args={"query": "cta"})),
                    ]
                )
            )
        ],
        text="",
    )


def _provider(response: _Response) -> tuple[GoogleGenAIProvider, _FakeClient]:
    client = _FakeClient(response)
    return GoogleGenAIProvider(config=GoogleProviderConfig(), client=client), client


def test_non_stream_tool_calls_get_distinct_ids_and_keep_response_order() -> None:
    provider, _client = _provider(_response_with_repeated_function_names())

    result = provider.propose_turn(_request())

    assert [call.tool_name for call in result.tool_calls] == ["read", "read", "grep"]
    assert [call.arguments for call in result.tool_calls] == [
        {"path": "a.txt"},
        {"path": "b.txt"},
        {"query": "cta"},
    ]
    ids = [call.tool_call_id for call in result.tool_calls]
    assert ids == ["read_1", "read_2", "grep_3"]
    assert len(set(ids)) == len(ids)


def test_stream_and_non_stream_agree_on_tool_call_ids_names_and_arguments() -> None:
    provider, client = _provider(_response_with_repeated_function_names())

    non_stream = provider.propose_turn(_request()).tool_calls
    events: list[ProviderStreamEvent] = list(provider.stream_turn(_request()))

    streamed: list[ToolCall] = []
    for event in events:
        if event.kind == "tool_call_end":
            assert event.tool_call_id is not None
            assert event.parsed_arguments is not None
            streamed.append(
                ToolCall(
                    tool_name=cast(str, event.tool_name),
                    arguments=event.parsed_arguments,
                    tool_call_id=event.tool_call_id,
                )
            )

    assert client.models.non_stream_calls == 1
    assert client.models.stream_calls == 1
    assert streamed == list(non_stream)


def _config_thinking(*, model_name: str, reasoning_effort: str | None, model_metadata: ProviderModelMetadata | None = None) -> Any:
    provider, client = _provider(_Response(candidates=[_Candidate(content=_Content(parts=[_Part(text="ok")]))]))
    provider.propose_turn(_request(model_name=model_name, reasoning_effort=reasoning_effort, model_metadata=model_metadata))
    config = client.models.last_config
    assert isinstance(config, types.GenerateContentConfig)
    return config.thinking_config


def test_reasoning_effort_sets_thinking_budget_for_gemini_2_family() -> None:
    thinking = _config_thinking(model_name="gemini-2.5-flash", reasoning_effort="high")

    assert thinking is not None
    assert thinking.thinking_budget == 8192
    assert thinking.include_thoughts is True
    assert thinking.thinking_level is None


def test_reasoning_effort_off_disables_thinking_for_gemini_2_family() -> None:
    thinking = _config_thinking(model_name="gemini-2.5-flash", reasoning_effort="off")

    assert thinking is not None
    assert thinking.thinking_budget == 0
    assert thinking.include_thoughts is False


def test_reasoning_effort_xhigh_and_max_collapse_to_the_high_budget() -> None:
    for effort in ("xhigh", "max"):
        thinking = _config_thinking(model_name="gemini-2.5-pro", reasoning_effort=effort)

        assert thinking is not None
        assert thinking.thinking_budget == 8192
        assert thinking.include_thoughts is True


def test_reasoning_effort_clamps_to_model_supported_levels() -> None:
    metadata = ProviderModelMetadata(supported_effort_levels=("minimal", "low"))
    thinking = _config_thinking(model_name="gemini-2.5-flash", reasoning_effort="high", model_metadata=metadata)

    assert thinking is not None
    assert thinking.thinking_budget == 2048


def test_reasoning_effort_sets_thinking_level_for_gemini_3() -> None:
    thinking = _config_thinking(model_name="gemini-3-pro-preview", reasoning_effort="medium")

    assert thinking is not None
    assert thinking.thinking_level == types.ThinkingLevel.MEDIUM
    assert thinking.include_thoughts is True
    assert thinking.thinking_budget is None


def test_reasoning_effort_off_uses_lowest_level_for_gemini_3() -> None:
    thinking = _config_thinking(model_name="gemini-3-flash-preview", reasoning_effort="off")

    assert thinking is not None
    assert thinking.thinking_level == types.ThinkingLevel.MINIMAL
    assert thinking.include_thoughts is False


def test_absent_reasoning_effort_leaves_thinking_config_unset() -> None:
    assert _config_thinking(model_name="gemini-2.5-flash", reasoning_effort=None) is None


def _response_with_thoughts() -> _Response:
    return _Response(
        candidates=[
            _Candidate(
                content=_Content(
                    parts=[
                        _Part(text="weighing options", thought=True),
                        _Part(text=" and checking files", thought=True),
                        _Part(text="final answer"),
                    ]
                )
            )
        ],
        text="final answer",
    )


def test_non_stream_surfaces_thought_parts_as_reasoning() -> None:
    provider, _client = _provider(_response_with_thoughts())

    result = provider.propose_turn(_request())

    assert result.reasoning == "weighing options and checking files"
    assert result.output == "final answer"


def test_stream_and_non_stream_agree_on_reasoning() -> None:
    provider, _client = _provider(_response_with_thoughts())

    result = provider.propose_turn(_request())
    events: list[ProviderStreamEvent] = list(provider.stream_turn(_request()))
    streamed_reasoning = "".join(
        cast(str, event.text) for event in events if event.kind == "delta" and event.channel == "reasoning" and event.text is not None
    )

    assert streamed_reasoning == result.reasoning


_SERVICE_ACCOUNT_PATH = "/etc/voidcode/service-account.json"


@dataclass
class _StubServiceAccountCredentials:
    project_id: str | None = None


def _record_sdk_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``genai.Client`` with a recorder that returns a working fake client."""
    observed: list[dict[str, Any]] = []
    client = _FakeClient(_Response(candidates=[], text="ok"))

    def _sdk_client(**kwargs: Any) -> _FakeClient:
        observed.append(kwargs)
        return client

    monkeypatch.setattr(genai, "Client", _sdk_client)
    return observed


def _stub_service_account_loader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    credentials: Any = None,
    error: Exception | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    observed: list[tuple[str, dict[str, Any]]] = []

    def _load(filename: str, **kwargs: Any) -> Any:
        observed.append((filename, kwargs))
        if error is not None:
            raise error
        return credentials

    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", _load)
    return observed


def _service_account_config(**overrides: Any) -> GoogleProviderConfig:
    auth = GoogleProviderAuthConfig(method="service_account", service_account_json_path=_SERVICE_ACCOUNT_PATH)
    return GoogleProviderConfig(auth=auth, **overrides)


def test_service_account_auth_passes_credentials_and_project_to_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = _StubServiceAccountCredentials(project_id="sa-project")
    loader_calls = _stub_service_account_loader(monkeypatch, credentials=credentials)
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=_service_account_config(project="configured-project", region="us-central1"))
    provider.propose_turn(_request())

    assert client_kwargs == [{"vertexai": True, "credentials": credentials, "project": "configured-project", "location": "us-central1"}]
    assert client_kwargs[0].get("api_key") is None
    assert loader_calls == [(_SERVICE_ACCOUNT_PATH, {"scopes": ["https://www.googleapis.com/auth/cloud-platform"]})]


def test_service_account_auth_ignores_ambient_google_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-google-key")
    credentials = _StubServiceAccountCredentials(project_id="sa-project")
    _stub_service_account_loader(monkeypatch, credentials=credentials)
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=_service_account_config(project="configured-project", region="us-central1"))
    provider.propose_turn(_request())

    assert client_kwargs[0]["credentials"] is credentials
    assert "ambient-google-key" not in str(client_kwargs)


def test_adc_project_and_region_enable_vertexai_without_any_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=GoogleProviderConfig(project="adc-project", region="us-central1"))
    provider.propose_turn(_request())

    assert client_kwargs == [{"vertexai": True, "project": "adc-project", "location": "us-central1"}]


def test_adc_project_alone_enables_vertexai_without_any_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=GoogleProviderConfig(project="adc-project"))
    provider.propose_turn(_request())

    assert client_kwargs == [{"vertexai": True, "project": "adc-project", "location": None}]


def test_adc_with_ambient_api_key_keeps_the_previous_project_region_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-google-key")
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=GoogleProviderConfig(project="adc-project", region="us-central1"))
    provider.propose_turn(_request())

    assert client_kwargs == [{"api_key": "ambient-google-key", "vertexai": True, "project": "adc-project", "location": "us-central1"}]


def test_service_account_project_is_backfilled_from_the_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = _StubServiceAccountCredentials(project_id="sa-project")
    _stub_service_account_loader(monkeypatch, credentials=credentials)
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=_service_account_config(region="europe-west4"))
    provider.propose_turn(_request())

    assert client_kwargs == [{"vertexai": True, "credentials": credentials, "project": "sa-project", "location": "europe-west4"}]


def test_service_account_without_project_or_region_enables_vertexai_from_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = _StubServiceAccountCredentials(project_id="sa-project")
    _stub_service_account_loader(monkeypatch, credentials=credentials)
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=_service_account_config())
    provider.propose_turn(_request())

    assert client_kwargs == [{"vertexai": True, "credentials": credentials, "project": "sa-project", "location": None}]


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(f"[Errno 2] No such file or directory: '{_SERVICE_ACCOUNT_PATH}'"),
        ValueError("Service account info was not in the expected format, missing fields client_email."),
    ],
    ids=["missing_file", "invalid_file"],
)
def test_unloadable_service_account_file_is_a_non_retryable_missing_auth_error(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    _stub_service_account_loader(monkeypatch, error=error)
    _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=_service_account_config(region="us-central1"))

    with pytest.raises(ProviderExecutionError) as caught:
        provider.propose_turn(_request())

    assert caught.value.kind == "missing_auth"
    assert caught.value.retryable is False
    assert caught.value.fallback_allowed is True
    assert _SERVICE_ACCOUNT_PATH not in caught.value.message
    assert str(error) not in caught.value.message


def test_unloadable_service_account_file_is_a_non_retryable_missing_auth_error_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_service_account_loader(monkeypatch, error=OSError("permission denied"))
    _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(config=_service_account_config(region="us-central1"))

    with pytest.raises(ProviderExecutionError) as caught:
        list(provider.stream_turn(_request()))

    assert caught.value.kind == "missing_auth"
    assert caught.value.retryable is False
    assert _SERVICE_ACCOUNT_PATH not in caught.value.message


def test_api_key_auth_still_passes_the_configured_key_and_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "ambient-google-key")
    client_kwargs = _record_sdk_client(monkeypatch)

    auth = GoogleProviderAuthConfig(method="api_key", api_key="configured-google-key")
    provider = GoogleGenAIProvider(config=GoogleProviderConfig(auth=auth))
    provider.propose_turn(_request())

    assert client_kwargs == [{"api_key": "configured-google-key"}]


def test_api_key_auth_with_project_alone_stays_on_the_gemini_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    client_kwargs = _record_sdk_client(monkeypatch)

    auth = GoogleProviderAuthConfig(method="api_key", api_key="configured-google-key")
    provider = GoogleGenAIProvider(config=GoogleProviderConfig(auth=auth, project="configured-project"))
    provider.propose_turn(_request())

    assert client_kwargs == [{"api_key": "configured-google-key"}]


def test_api_key_auth_with_project_and_region_keeps_vertex_express_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    client_kwargs = _record_sdk_client(monkeypatch)

    auth = GoogleProviderAuthConfig(method="api_key", api_key="configured-google-key")
    provider = GoogleGenAIProvider(config=GoogleProviderConfig(auth=auth, project="configured-project", region="us-central1"))
    provider.propose_turn(_request())

    assert client_kwargs == [{"api_key": "configured-google-key", "vertexai": True, "project": "configured-project", "location": "us-central1"}]


@dataclass(slots=True)
class _SdkWire:
    """The real SDK client's requests, and the clients it built."""

    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[genai.Client] = field(default_factory=list)


def _record_sdk_wire(monkeypatch: pytest.MonkeyPatch) -> _SdkWire:
    """Replace ``genai.Client`` with the real client wired to a mock transport.

    Unlike ``_record_sdk_client`` this keeps the SDK's own URL join inside the
    assertion path, so a configured base URL is observed as the request it sends.
    """
    wire = _SdkWire()
    real_constructor = genai.Client

    def respond(request: httpx.Request) -> httpx.Response:
        wire.requests.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})

    http_client = httpx.Client(transport=httpx.MockTransport(respond))

    def _sdk_client(**kwargs: Any) -> genai.Client:
        options = kwargs.pop("http_options", None) or types.HttpOptions()
        client = real_constructor(http_options=options.model_copy(update={"httpx_client": http_client}), **kwargs)
        wire.clients.append(client)
        return client

    monkeypatch.setattr(genai, "Client", _sdk_client)
    return wire


def test_configured_base_url_drives_the_request_url_and_keeps_the_api_key_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = _record_sdk_wire(monkeypatch)
    auth = GoogleProviderAuthConfig(method="api_key", api_key="gateway-key")
    provider = GoogleGenAIProvider(config=GoogleProviderConfig(auth=auth, base_url="https://gateway.example.test/v1"))

    result = provider.propose_turn(_request(model_name="gemini-3-flash"))

    # A configured base URL is the endpoint root: the SDK appends only the
    # resource path, never its own ``v1beta`` version segment.
    assert [str(request.url) for request in wire.requests] == ["https://gateway.example.test/v1/models/gemini-3-flash:generateContent"]
    assert wire.requests[0].headers["x-goog-api-key"] == "gateway-key"
    assert "authorization" not in wire.requests[0].headers
    # An api_key endpoint must not be reported as (or behave like) Vertex AI.
    assert wire.clients[0].vertexai is False
    assert result.output == "ok"


def test_configured_base_url_is_honoured_on_the_vertexai_path_too(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = _StubServiceAccountCredentials(project_id="sa-project")
    _stub_service_account_loader(monkeypatch, credentials=credentials)
    client_kwargs = _record_sdk_client(monkeypatch)

    provider = GoogleGenAIProvider(
        config=_service_account_config(project="configured-project", region="us-central1", base_url="https://vertex-proxy.example.test")
    )

    provider.propose_turn(_request())

    assert client_kwargs == [
        {
            "vertexai": True,
            "credentials": credentials,
            "project": "configured-project",
            "location": "us-central1",
            "http_options": types.HttpOptions(base_url="https://vertex-proxy.example.test", api_version=""),
        }
    ]


_GATEWAY_HEADERS = {"x-opencode-session": "{session_id}", "x-opencode-client": "voidcode"}


def _gateway_provider() -> GoogleGenAIProvider:
    auth = GoogleProviderAuthConfig(method="api_key", api_key="gateway-key")
    return GoogleGenAIProvider(
        config=GoogleProviderConfig(auth=auth, base_url="https://gateway.example.test/v1"),
        extra_request_headers=_GATEWAY_HEADERS,
    )


def _header(request: httpx.Request, name: str) -> str | None:
    return cast(str | None, request.headers.get(name))


def test_declared_request_headers_reach_the_wire_resolved_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared header rides on the request config, leaving the client untouched.

    The SDK patches a request-level ``http_options`` over the client's own options
    for that request only, so a session-scoped value must not need a
    session-scoped client -- and the client's ``Content-Type``/credential headers
    must survive the patch.
    """
    wire = _record_sdk_wire(monkeypatch)
    provider = _gateway_provider()

    result = provider.propose_turn(_request(model_name="gemini-3-flash", session_id="conversation-a"))

    assert [str(request.url) for request in wire.requests] == ["https://gateway.example.test/v1/models/gemini-3-flash:generateContent"]
    assert _header(wire.requests[0], "x-opencode-session") == "conversation-a"
    assert _header(wire.requests[0], "x-opencode-client") == "voidcode"
    # The gateway's own headers are merged, not replaced, and the request body
    # never carries the request config.
    assert _header(wire.requests[0], "content-type") == "application/json"
    assert _header(wire.requests[0], "x-goog-api-key") == "gateway-key"
    assert "http_options" not in wire.requests[0].content.decode("utf-8")
    # One client serves every conversation: the header is per request, not per client.
    assert len(wire.clients) == 1
    assert result.output == "ok"

    _ = provider.propose_turn(_request(model_name="gemini-3-flash", session_id="conversation-b"))

    assert _header(wire.requests[1], "x-opencode-session") == "conversation-b"
    assert len(wire.clients) == 1

    events = list(provider.stream_turn(_request(model_name="gemini-3-flash", session_id="conversation-c")))

    assert [str(request.url) for request in wire.requests[2:]] == [
        "https://gateway.example.test/v1/models/gemini-3-flash:streamGenerateContent?alt=sse"
    ]
    assert _header(wire.requests[2], "x-opencode-session") == "conversation-c"
    assert len(wire.clients) == 1
    assert [event.text for event in events if event.kind == "delta"] == ["ok"]

    # No session id: the header names nothing, so it is omitted -- not sent empty.
    _ = provider.propose_turn(_request(model_name="gemini-3-flash"))

    assert _header(wire.requests[3], "x-opencode-session") is None
    assert _header(wire.requests[3], "x-opencode-client") == "voidcode"


def test_gateway_request_headers_are_only_sent_by_the_provider_that_declares_them(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = _record_sdk_wire(monkeypatch)
    auth = GoogleProviderAuthConfig(method="api_key", api_key="gateway-key")
    provider = GoogleGenAIProvider(config=GoogleProviderConfig(auth=auth, base_url="https://gateway.example.test/v1"))

    _ = provider.propose_turn(_request(model_name="gemini-3-flash", session_id="conversation-a"))

    assert _header(wire.requests[0], "x-opencode-session") is None
    assert _header(wire.requests[0], "x-opencode-client") is None
