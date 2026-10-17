from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import output_text
from voidcode.tools.contracts import ToolCall
from voidcode.tools.web_search import WebSearchItem, WebSearchResultBody, WebSearchTool


@pytest.fixture(autouse=True)
def _isolate_search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.delenv("KAGI_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_BASE_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.delenv("KIMI_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_SEARCH_BASE_URL", raising=False)
    monkeypatch.delenv("MOONSHOT_SEARCH_BASE_URL", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.delenv("SYNTHETIC_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_ENDPOINT", raising=False)
    monkeypatch.delenv("SEARXNG_TOKEN", raising=False)
    monkeypatch.delenv("SEARXNG_BASIC_USERNAME", raising=False)
    monkeypatch.delenv("SEARXNG_BASIC_PASSWORD", raising=False)
    monkeypatch.delenv("VOIDCODE_WEB_SEARCH_ORDER", raising=False)
    monkeypatch.delenv("VOIDCODE_WEB_SEARCH_EXCLUDE", raising=False)


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    client_type = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "voidcode.tools.web_search.httpx.Client",
        lambda *args, **kwargs: client_type(*args, transport=transport, **kwargs),
    )


def _json_response(payload: Mapping[str, object], request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=payload, request=request)


def _html_response(html: str, request: httpx.Request, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=html, request=request)


def test_websearch_tool_rejects_empty_query() -> None:
    tool = WebSearchTool()

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "   "}),
            context=ToolContext(workspace=Path("/tmp")),
        )


@pytest.mark.parametrize(
    ("arguments", "expected_num_results"),
    [({"query": "test", "numResults": 5}, 5), ({"query": "test"}, 8)],
)
def test_websearch_tool_returns_structured_exa_results(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object], expected_num_results: int
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
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments=arguments),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.num_results == expected_num_results
    assert result.body.source == "exa"
    assert result.body.fallback_reason is None
    assert result.body.results == (WebSearchItem(1, "Example", "https://example.com", snippet),)
    assert result.output.bounds.source == "exa"
    visible_text = output_text(result.output)
    assert visible_text is not None
    assert visible_text == f"Search source: exa\n\n1. Example\n   https://example.com\n   {snippet}\n"
    assert not visible_text.endswith("...")


def test_websearch_tool_uses_beautifulsoup_ddg_fallback_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_ORDER", "duckduckgo")
    html = """
    <html>
      <body>
        <div class="result">
          <h2 class="result__title">
            <a class="result__a"
               href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
              Result A
            </a>
          </h2>
          <div class="result__snippet">
            <span>Snippet</span> <strong>A</strong>
          </div>
        </div>
        <article>
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb">
            Result B
          </a>
          <div class="result__snippet">Snippet B</div>
        </article>
      </body>
    </html>
    """
    _mock_transport(monkeypatch, lambda request: _html_response(html, request))
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test", "numResults": 2}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "duckduckgo"
    assert result.body.results == (
        WebSearchItem(1, "Result A", "https://example.com/a", "Snippet A"),
        WebSearchItem(2, "Result B", "https://example.org/b", "Snippet B"),
    )
    assert result.output.bounds.source == "duckduckgo"
    assert output_text(result.output) == (
        "Search source: duckduckgo\n\n1. Result A\n   https://example.com/a\n   Snippet A\n\n2. Result B\n   https://example.org/b\n   Snippet B\n"
    )


def test_websearch_tool_reports_exa_empty_result_fallback_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_ORDER", "exa,duckduckgo")
    html = '<div class="result"><a class="result__a" href="https://example.com">Example</a></div>'

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response({"results": []}, request)
        return _html_response(html, request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "duckduckgo"
    assert result.body.fallback_reason == "exa search returned no results"
    assert (output_text(result.output) or "").startswith("Search source: duckduckgo | fallback: exa search returned no results")


def test_websearch_tool_treats_no_results_as_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_EXCLUDE", "firecrawl,startpage")
    _mock_transport(monkeypatch, lambda request: _html_response("<html><body>No results</body></html>", request))
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "missing"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "duckduckgo-empty"
    assert result.body.results == ()
    assert result.output.bounds.fallback_reason == "duckduckgo fallback returned no parseable results"
    assert (output_text(result.output) or "").startswith(
        "Search source: duckduckgo-empty | fallback: duckduckgo fallback returned no parseable results"
    )


def test_websearch_tool_reports_error_when_exa_and_duckduckgo_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_EXCLUDE", "firecrawl,startpage")

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    _mock_transport(monkeypatch, fail)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "error"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "duckduckgo-error"
    assert result.body.results == ()
    assert result.output.bounds.source == "duckduckgo-error"
    assert result.error is not None and result.error.startswith("All web search providers failed:")
    assert (output_text(result.output) or "").startswith("Search source: duckduckgo-error | fallback: exa search failed:")


def test_websearch_tool_returns_structured_tavily_results(monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "python async"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "tavily"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(
        1, "Python Async Guide", "https://example.com/python-async", "A comprehensive guide to asyncio in Python."
    )
    assert result.output.bounds.source == "tavily"
    assert (output_text(result.output) or "").startswith("Search source: tavily")


def test_websearch_tool_returns_structured_brave_results(monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "brave search", "numResults": 5}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "brave"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Brave Search Engine", "https://search.brave.com", "Private, independent search engine.")
    assert result.output.bounds.source == "brave"
    assert (output_text(result.output) or "").startswith("Search source: brave")


def test_websearch_tool_returns_structured_deepseek_results(monkeypatch: pytest.MonkeyPatch) -> None:
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
                        "rerank_score": 2.4,
                        "title": "DeepSeek V4 model released",
                        "url": "https://example.com/deepseek-v4",
                        "snippet": "DeepSeek V4 is now available.",
                        "summary": "DeepSeek has released the V4 model.",
                        "content": "",
                        "site_name": "Example News",
                        "site_icon": None,
                        "date": None,
                        "authority": 3,
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "deepseek v4"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "deepseek"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(
        1, "DeepSeek V4 model released", "https://example.com/deepseek-v4", "DeepSeek V4 is now available."
    )
    assert result.output.bounds.source == "deepseek"
    assert (output_text(result.output) or "").startswith("Search source: deepseek")


def test_websearch_tool_skips_deepseek_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_ORDER", "deepseek,duckduckgo")

    def handle(request: httpx.Request) -> httpx.Response:
        assert "duckduckgo" in str(request.url)
        return _html_response("<html><body>No results</body></html>", request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "deepseek v4"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert result.body is not None and result.body.source == "duckduckgo-empty"


def test_websearch_tool_respects_order_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-key")
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_ORDER", "brave,exa")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return _json_response(
                {"web": {"results": [{"title": "Brave Result", "url": "https://brave.com", "description": "From Brave"}]}},
                request,
            )
        return httpx.Response(500, request=request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert result.body is not None and result.body.source == "brave"


def test_websearch_tool_ignores_unknown_providers_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_ORDER", "unknown_service,tavily")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return _json_response(
                {"results": [{"title": "Tavily Hit", "url": "https://tavily.com", "content": "Tavily"}]},
                request,
            )
        return httpx.Response(500, request=request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert result.body is not None and result.body.source == "tavily"


def test_websearch_tool_respects_exclude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_EXCLUDE", "exa")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return _json_response(
                {"results": [{"title": "Tavily Result", "url": "https://tavily.com", "content": "Desc"}]},
                request,
            )
        return httpx.Response(500, request=request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert result.body is not None and result.body.source == "tavily"


def test_websearch_tool_aggregates_fallback_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    html = '<div class="result"><a class="result__a" href="https://example.com/ddg">DDG Result</a></div>'

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return httpx.Response(500, request=request)
        if request.url.host == "api.tavily.com":
            return httpx.Response(502, request=request)
        return _html_response(html, request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert result.body is not None and result.body.source == "duckduckgo"
    fallback_reason = result.output.bounds.fallback_reason or ""
    assert "exa search failed:" in fallback_reason
    assert "tavily search failed:" in fallback_reason
    assert "; " in fallback_reason
    assert (output_text(result.output) or "").startswith(f"Search source: duckduckgo | fallback: {fallback_reason}")


def test_websearch_tool_reports_all_fail_error_with_aggregated_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("BRAVE_API_KEY", "brave-key")

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    _mock_transport(monkeypatch, fail)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "error"
    assert result.body is not None and result.body.source == "duckduckgo-error"
    assert result.error is not None and result.error.startswith("All web search providers failed:")
    assert "exa search failed:" in result.error
    assert "tavily search failed:" in result.error
    assert "brave search failed:" in result.error
    assert "duckduckgo fallback failed:" in result.error


def test_websearch_tool_reports_error_when_no_provider_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_EXCLUDE", "duckduckgo,startpage,firecrawl")

    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "error"
    assert result.body is not None and result.body.source == "none"
    assert result.error == "No web search provider configured."


def test_websearch_tool_supports_internal_provider_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return _json_response(
                {"results": [{"title": "Forced Tavily", "url": "https://tavily.com", "content": "Content"}]},
                request,
            )
        return httpx.Response(500, request=request)

    _mock_transport(monkeypatch, handle)
    tool = WebSearchTool()
    call = ToolCall(tool_name="web_search", arguments={"query": "test"})
    result = tool._invoke(call, runtime_timeout_seconds=None, provider="tavily")

    assert result.status == "ok"
    assert result.body is not None and result.body.source == "tavily"


def test_websearch_tool_rejects_unknown_provider_override() -> None:
    tool = WebSearchTool()
    call = ToolCall(tool_name="web_search", arguments={"query": "test"})
    result = tool._invoke(call, runtime_timeout_seconds=None, provider="nonexistent")

    assert result.status == "error"
    assert result.body is not None and result.body.source == "none"
    assert "Unknown web search provider: 'nonexistent'" in (result.error or "")


def test_websearch_tool_returns_structured_tinyfish_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TINYFISH_API_KEY", "test-tinyfish-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.search.tinyfish.ai"
        assert request.headers.get("X-API-Key") == "test-tinyfish-key"
        assert request.url.params["query"] == "tinyfish search"
        assert request.url.params["num_results"] == "5"
        return _json_response(
            {
                "results": [
                    {
                        "title": "TinyFish Result",
                        "url": "https://example.com/tinyfish",
                        "snippet": "TinyFish search snippet.",
                        "site_name": "TinyFish Site",
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "tinyfish search", "numResults": 5}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "tinyfish"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "TinyFish Result", "https://example.com/tinyfish", "TinyFish search snippet.")
    assert result.output.bounds.source == "tinyfish"
    assert (output_text(result.output) or "").startswith("Search source: tinyfish")


def test_websearch_tool_returns_structured_jina_results(monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "jina search", "numResults": 5}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "jina"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Jina Result", "https://example.com/jina", "Jina reader search snippet.")
    assert result.output.bounds.source == "jina"
    assert (output_text(result.output) or "").startswith("Search source: jina")


def test_websearch_tool_returns_structured_kagi_results(monkeypatch: pytest.MonkeyPatch) -> None:
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
                    ]
                }
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "kagi search", "numResults": 5}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "kagi"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Kagi Result", "https://example.com/kagi", "Kagi search snippet.")
    assert result.output.bounds.source == "kagi"
    assert (output_text(result.output) or "").startswith("Search source: kagi")


def test_websearch_tool_returns_structured_firecrawl_results(monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "firecrawl search", "numResults": 5}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "firecrawl"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Firecrawl Result", "https://example.com/firecrawl", "Firecrawl search snippet.")
    assert result.output.bounds.source == "firecrawl"
    assert (output_text(result.output) or "").startswith("Search source: firecrawl")


def test_websearch_tool_returns_structured_firecrawl_keyless_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.firecrawl.dev/v2/search"
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert body["query"] == "keyless search"
        return _json_response(
            {
                "success": True,
                "data": [
                    {
                        "title": "Keyless Firecrawl",
                        "url": "https://example.com/keyless",
                        "description": "Keyless snippet.",
                    }
                ],
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "keyless search"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "firecrawl"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Keyless Firecrawl", "https://example.com/keyless", "Keyless snippet.")


def test_websearch_tool_returns_structured_kimi_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMI_SEARCH_API_KEY", "test-kimi-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.kimi.com/coding/v1/search"
        assert request.headers.get("Authorization") == "Bearer test-kimi-key"
        body = json.loads(request.content)
        assert body["text_query"] == "kimi search"
        assert body["limit"] == 5
        return _json_response(
            {
                "search_results": [
                    {
                        "title": "Kimi Result",
                        "url": "https://example.com/kimi",
                        "snippet": "Kimi search snippet.",
                        "site_name": "Kimi Site",
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "kimi search", "numResults": 5}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "kimi"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Kimi Result", "https://example.com/kimi", "Kimi search snippet.")
    assert result.output.bounds.source == "kimi"
    assert (output_text(result.output) or "").startswith("Search source: kimi")


def test_websearch_tool_returns_structured_parallel_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARALLEL_API_KEY", "test-parallel-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.parallel.ai/v1beta/search"
        assert request.headers.get("x-api-key") == "test-parallel-key"
        assert request.headers.get("parallel-beta") == "search-extract-2025-10-10"
        body = json.loads(request.content)
        assert body["objective"] == "parallel search"
        assert body["search_queries"] == ["parallel search"]
        assert body["mode"] == "fast"
        return _json_response(
            {
                "search_id": "test-parallel-id",
                "results": [
                    {
                        "title": "Parallel Result",
                        "url": "https://example.com/parallel",
                        "excerpts": ["Parallel search snippet excerpt."],
                    }
                ],
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "parallel search", "numResults": 5}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "parallel"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Parallel Result", "https://example.com/parallel", "Parallel search snippet excerpt.")
    assert result.output.bounds.source == "parallel"
    assert (output_text(result.output) or "").startswith("Search source: parallel")


def test_websearch_tool_returns_structured_synthetic_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHETIC_API_KEY", "test-synthetic-key")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.synthetic.new/v2/search"
        assert request.headers.get("Authorization") == "Bearer test-synthetic-key"
        body = json.loads(request.content)
        assert body["query"] == "synthetic search"
        return _json_response(
            {
                "results": [
                    {
                        "title": "Synthetic Result",
                        "url": "https://example.com/synthetic",
                        "text": "Synthetic search text snippet.",
                    }
                ]
            },
            request,
        )

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "synthetic search"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "synthetic"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Synthetic Result", "https://example.com/synthetic", "Synthetic search text snippet.")
    assert result.output.bounds.source == "synthetic"
    assert (output_text(result.output) or "").startswith("Search source: synthetic")


def test_websearch_tool_returns_structured_searxng_results(monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "searxng search"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "searxng"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "SearXNG Result", "https://example.com/searxng", "SearXNG content snippet.")
    assert result.output.bounds.source == "searxng"
    assert (output_text(result.output) or "").startswith("Search source: searxng")


def test_websearch_tool_skips_searxng_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARXNG_ENDPOINT", raising=False)
    monkeypatch.setenv("VOIDCODE_WEB_SEARCH_ORDER", "searxng,duckduckgo")

    def handle(request: httpx.Request) -> httpx.Response:
        assert "duckduckgo" in str(request.url)
        return _html_response("<html><body>No results</body></html>", request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "test query"}),
        context=ToolContext(workspace=Path("/tmp")),
    )

    assert result.status == "ok"
    assert result.body is not None and result.body.source == "duckduckgo-empty"


def test_websearch_tool_returns_structured_startpage_results(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <html>
      <body>
        <div class="result">
          <a class="result-link" href="https://example.com/startpage-item">
            <h2 class="wgl-title">Startpage Title</h2>
          </a>
          <p class="description">Startpage snippet description.</p>
        </div>
      </body>
    </html>
    """

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.startpage.com"
        assert request.url.path == "/sp/search"
        assert request.url.params["query"] == "startpage search"
        return _html_response(html, request)

    _mock_transport(monkeypatch, handle)
    result = WebSearchTool().invoke(
        ToolCall(tool_name="web_search", arguments={"query": "startpage search"}),
        context=ToolContext(workspace=Path("/tmp")),
        provider="startpage",
    )

    assert result.status == "ok"
    assert isinstance(result.body, WebSearchResultBody)
    assert result.body.source == "startpage"
    assert len(result.body.results) == 1
    assert result.body.results[0] == WebSearchItem(1, "Startpage Title", "https://example.com/startpage-item", "Startpage snippet description.")
    assert result.output.bounds.source == "startpage"
    assert (output_text(result.output) or "").startswith("Search source: startpage")
