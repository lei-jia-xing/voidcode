"""Unit tests for the web plugin's ``web_search`` tool."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from voidcode.plugins.web import create_plugin
from voidcode.plugins.web.web_search import WebSearchItem, WebSearchResultBody, WebSearchTool
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import ToolCall, ToolFailure, ToolResult, ToolSuccess


class _DummyServices:
    """Core service bag stand-in: web_search never touches core services."""


_SERVICES = cast(Any, _DummyServices())

_SEARCH_ENV_VARS = (
    "EXA_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "DEEPSEEK_API_KEY",
    "TINYFISH_API_KEY",
    "JINA_API_KEY",
    "KAGI_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_BASE_URL",
    "FIRECRAWL_API_URL",
    "SEARXNG_ENDPOINT",
    "SEARXNG_TOKEN",
    "SEARXNG_BASIC_USERNAME",
    "SEARXNG_BASIC_PASSWORD",
)


@pytest.fixture(autouse=True)
def _isolate_search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SEARCH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tool_context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        workspace=tmp_path,
        session_id="s",
        run_id="r",
        invocation_id="i",
        revision_id="rev",
        actor="model",
    )


def _web_search_tool() -> WebSearchTool:
    return cast(WebSearchTool, {tool.definition.name: tool for tool in create_plugin({}).tools}["web_search"])


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    client_type = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "voidcode.plugins.web.web_search.httpx.Client",
        lambda *args, **kwargs: client_type(*args, transport=transport, **kwargs),
    )


def _json_response(payload: Mapping[str, object], request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=payload, request=request)


def _run_search(context: ToolContext, arguments: dict[str, object]) -> ToolResult:
    return _web_search_tool().invoke(
        ToolCall(tool_name="web_search", arguments=arguments),
        context=context,
        services=_SERVICES,
    )


def _assert_single_result(result: ToolResult, source: str, item: WebSearchItem) -> None:
    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == source
    assert result.body.results == (item,)
    assert result.output.bounds.source == source
    assert f"1. {item.title}" in result.output.text and item.url in result.output.text


def test_websearch_tool_rejects_empty_query(tool_context: ToolContext) -> None:
    with pytest.raises(ValueError):
        _run_search(tool_context, {"query": "   "})


@pytest.mark.parametrize(
    ("arguments", "expected_num_results"),
    [({"query": "test", "numResults": 5}, 5), ({"query": "test"}, 8)],
)
def test_websearch_tool_returns_structured_exa_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
    expected_num_results: int,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    snippet = "selected-highlight-" * 20

    def handle(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content) if request.content else {}
        if (
            request.method == "POST"
            and request_body.get("query") == "test"
            and request_body.get("type") == "auto"
            and request_body.get("contents") == {"highlights": True}
        ):
            return _json_response(
                {"results": [{"title": "Example", "url": "https://example.com", "highlights": [snippet]}]},
                request,
            )
        return httpx.Response(400 if request.method == "POST" else 503, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run_search(tool_context, arguments)

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.num_results == expected_num_results
    assert result.body.source == "exa"
    assert result.body.results == (WebSearchItem(1, "Example", "https://example.com", snippet),)
    assert result.output.bounds.source == "exa"


def test_websearch_tool_returns_structured_tavily_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.headers.get("Authorization") == "Bearer test-tavily-key"
        body = json.loads(request.content)
        assert body["query"] == "python async"
        assert body["max_results"] == 8
        return _json_response(
            {
                "results": [
                    {
                        "title": "Python Async Guide",
                        "url": "https://example.com/python-async",
                        "content": "A comprehensive guide to asyncio in Python.",
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "python async"}),
        "tavily",
        WebSearchItem(1, "Python Async Guide", "https://example.com/python-async", "A comprehensive guide to asyncio in Python."),
    )


def test_websearch_tool_returns_structured_brave_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/res/v1/web/search"
        assert request.headers.get("X-Subscription-Token") == "test-brave-key"
        assert request.url.params["q"] == "brave search"
        assert request.url.params["count"] == "5"
        return _json_response(
            {
                "web": {
                    "results": [
                        {
                            "title": "Brave Search Engine",
                            "url": "https://search.brave.com",
                            "description": "Private, independent search engine.",
                        }
                    ]
                }
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "brave search", "numResults": 5}),
        "brave",
        WebSearchItem(1, "Brave Search Engine", "https://search.brave.com", "Private, independent search engine."),
    )


def test_websearch_tool_returns_structured_deepseek_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers.get("Authorization") == "Bearer test-deepseek-key"
        body = json.loads(request.content)
        assert body["query"] == "deepseek v4"
        assert body["count"] == 8
        return _json_response(
            {
                "results": [
                    {
                        "title": "DeepSeek V4 model released",
                        "url": "https://example.com/deepseek-v4",
                        "snippet": "DeepSeek V4 is now available.",
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "deepseek v4"}),
        "deepseek",
        WebSearchItem(1, "DeepSeek V4 model released", "https://example.com/deepseek-v4", "DeepSeek V4 is now available."),
    )


def test_websearch_tool_truncates_deepseek_query_to_128_chars(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    seen: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _json_response({"results": []}, request)

    _mock_transport(monkeypatch, handle)
    _run_search(tool_context, {"query": "x" * 200})

    assert seen["query"] == "x" * 128


def test_websearch_tool_returns_structured_tinyfish_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "test-tinyfish-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.search.tinyfish.ai"
        assert request.headers.get("X-API-Key") == "test-tinyfish-key"
        assert request.url.params["query"] == "tinyfish search"
        assert "num_results" not in request.url.params
        return _json_response(
            {
                "results": [
                    {
                        "title": "TinyFish Result",
                        "url": "https://example.com/tinyfish",
                        "snippet": "TinyFish search snippet.",
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "tinyfish search", "numResults": 5}),
        "tinyfish",
        WebSearchItem(1, "TinyFish Result", "https://example.com/tinyfish", "TinyFish search snippet."),
    )


def test_websearch_tool_returns_structured_jina_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "s.jina.ai"
        assert request.headers.get("Authorization") == "Bearer test-jina-key"
        assert request.headers.get("X-Respond-With") == "no-content"
        assert request.url.params["count"] == "5"
        return _json_response(
            {
                "code": 200,
                "data": [
                    {
                        "title": "Jina Result",
                        "url": "https://example.com/jina",
                        "description": "Jina reader search snippet.",
                    }
                ],
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "jina search", "numResults": 5}),
        "jina",
        WebSearchItem(1, "Jina Result", "https://example.com/jina", "Jina reader search snippet."),
    )


def test_websearch_tool_returns_structured_kagi_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAGI_API_KEY", "test-kagi-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://kagi.com/api/v1/search"
        assert request.headers.get("Authorization") == "Bearer test-kagi-key"
        body = json.loads(request.content)
        assert body["query"] == "kagi search"
        assert body["limit"] == 5
        assert body["workflow"] == "search"
        return _json_response(
            {
                "data": {
                    "search": [
                        {
                            "title": "Kagi Result",
                            "url": "https://example.com/kagi",
                            "snippet": "Kagi search snippet.",
                        }
                    ],
                    "news": [
                        {
                            "title": "Kagi News",
                            "url": "https://example.com/kagi-news",
                            "snippet": "Kagi news snippet.",
                        }
                    ],
                }
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = _run_search(tool_context, {"query": "kagi search", "numResults": 5})

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "kagi"
    assert result.body.results == (
        WebSearchItem(1, "Kagi Result", "https://example.com/kagi", "Kagi search snippet."),
        WebSearchItem(2, "Kagi News", "https://example.com/kagi-news", "Kagi news snippet."),
    )


def test_websearch_tool_returns_structured_firecrawl_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-firecrawl-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.firecrawl.dev/v2/search"
        assert request.headers.get("Authorization") == "Bearer test-firecrawl-key"
        body = json.loads(request.content)
        assert body["query"] == "firecrawl search"
        assert body["limit"] == 5
        assert body["sources"] == [{"type": "web"}]
        return _json_response(
            {
                "success": True,
                "data": [
                    {
                        "title": "Firecrawl Result",
                        "url": "https://example.com/firecrawl",
                        "description": "Firecrawl search snippet.",
                    }
                ],
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "firecrawl search", "numResults": 5}),
        "firecrawl",
        WebSearchItem(1, "Firecrawl Result", "https://example.com/firecrawl", "Firecrawl search snippet."),
    )


def test_websearch_tool_returns_structured_searxng_results(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARXNG_ENDPOINT", "https://searxng.local")
    monkeypatch.setenv("SEARXNG_TOKEN", "test-searxng-token")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "searxng.local"
        assert request.url.path == "/search"
        assert request.headers.get("Authorization") == "Bearer test-searxng-token"
        assert request.url.params["q"] == "searxng search"
        assert request.url.params["format"] == "json"
        return _json_response(
            {
                "results": [
                    {
                        "title": "SearXNG Result",
                        "url": "https://example.com/searxng",
                        "content": "SearXNG content snippet.",
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "searxng search"}),
        "searxng",
        WebSearchItem(1, "SearXNG Result", "https://example.com/searxng", "SearXNG content snippet."),
    )


def test_websearch_tool_prefers_first_available_provider(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.tavily.com/search"
        return _json_response({"results": []}, request)

    _mock_transport(monkeypatch, handle)
    result = _run_search(tool_context, {"query": "test"})

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "tavily"


def test_websearch_tool_uses_keyless_jina_without_any_credential(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "s.jina.ai"
        assert "Authorization" not in request.headers
        return _json_response(
            {"code": 200, "data": [{"title": "Keyless Jina", "url": "https://example.com/keyless", "description": "No key needed."}]},
            request,
        )

    _mock_transport(monkeypatch, handle)
    _assert_single_result(
        _run_search(tool_context, {"query": "test"}),
        "jina",
        WebSearchItem(1, "Keyless Jina", "https://example.com/keyless", "No key needed."),
    )


def test_websearch_tool_reports_keyless_jina_refusal_without_fallback(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "s.jina.ai"
        return httpx.Response(429, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run_search(tool_context, {"query": "test"})

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "jina"


def test_websearch_tool_winner_failure_is_final(
    tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert "api.exa.ai" in str(request.url)
        return httpx.Response(503, request=request)

    _mock_transport(monkeypatch, handle)
    result = _run_search(tool_context, {"query": "test"})

    assert isinstance(result, ToolFailure)
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "exa"


def test_websearch_tool_bounds_error_messages(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("internal-socket-detail")

    _mock_transport(monkeypatch, fail)
    result = _run_search(tool_context, {"query": "test"})

    assert isinstance(result, ToolFailure)
    assert "internal-socket-detail" not in result.error


def test_websearch_tool_reports_empty_results(tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    _mock_transport(monkeypatch, lambda request: _json_response({"results": []}, request))

    result = _run_search(tool_context, {"query": "missing"})

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.results == ()
