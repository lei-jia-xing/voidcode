from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import ToolCall
from voidcode.tools.web_search import WebSearchTool


def _json_response(payload: Mapping[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://api.exa.ai/search"),
    )


def _html_response(html: str) -> httpx.Response:
    return httpx.Response(
        200,
        text=html,
        request=httpx.Request("GET", "https://html.duckduckgo.com/html/?q=test&kl=wt-wt"),
    )


def _failing_response() -> httpx.Response:
    request = httpx.Request("GET", "https://html.duckduckgo.com/html/?q=test&kl=wt-wt")
    return httpx.Response(503, request=request)


def test_websearch_tool_rejects_empty_query() -> None:
    tool = WebSearchTool()

    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "   "}),
            context=ToolContext(workspace=Path("/tmp")),
        )


def test_websearch_tool_respects_num_results_limit() -> None:
    tool = WebSearchTool()

    fake_response = {"results": [{"title": "Example", "url": "https://example.com", "snippet": "snippet"}]}

    with (
        patch.dict("os.environ", {"EXA_API_KEY": "test-key"}, clear=False),
        patch(
            "httpx.Client.post",
            return_value=_json_response(fake_response),
        ),
    ):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test", "numResults": 5}),
            context=ToolContext(workspace=Path("/tmp")),
        )

    assert result.data["num_results"] == 5


def test_websearch_tool_defaults_to_8_results() -> None:
    tool = WebSearchTool()

    fake_response = {"results": [{"title": "Example", "url": "https://example.com", "snippet": "snippet"}]}

    with (
        patch.dict("os.environ", {"EXA_API_KEY": "test-key"}, clear=False),
        patch(
            "httpx.Client.post",
            return_value=_json_response(fake_response),
        ),
    ):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test"}),
            context=ToolContext(workspace=Path("/tmp")),
        )

    assert result.data["num_results"] == 8


def test_websearch_tool_uses_beautifulsoup_ddg_fallback_parsing() -> None:
    tool = WebSearchTool()

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

    with patch("httpx.Client.get", return_value=_html_response(html)):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test", "numResults": 2}),
            context=ToolContext(workspace=Path("/tmp")),
        )

    assert result.status == "ok"
    assert result.data["source"] == "duckduckgo"
    assert isinstance(result.content, str)
    lines = str(result.data["results"]).splitlines()
    assert lines[0] == "1. Result A"
    assert lines[1] == "   https://example.com/a"
    assert lines[2] == "   Snippet A..."
    assert lines[4] == "2. Result B"
    assert lines[5] == "   https://example.org/b"
    assert lines[6] == "   Snippet B..."


def test_websearch_tool_reports_truthful_metadata_when_ddg_parsing_fails() -> None:
    tool = WebSearchTool()

    with patch("httpx.Client.get", return_value=_failing_response()):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test"}),
            context=ToolContext(workspace=Path("/tmp")),
        )

    assert result.status == "error"
    assert result.data["source"] == "duckduckgo-error"
    assert result.error is not None
    assert result.error.startswith("Web search failed:")
    assert result.fallback_reason is not None
    assert result.fallback_reason.startswith("duckduckgo fallback failed:")
