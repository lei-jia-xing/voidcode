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
    assert result.error is not None and result.error.startswith("Web search failed:")
    assert (output_text(result.output) or "").startswith("Search source: duckduckgo-error | fallback: exa search failed:")
