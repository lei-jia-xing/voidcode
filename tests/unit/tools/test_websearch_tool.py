from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WebSearchTool


def _json_response(payload: Mapping[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://api.exa.ai/search"),
    )


def test_websearch_tool_rejects_empty_query() -> None:
    tool = WebSearchTool()
    empty_query_error = (
        r"web_search invalid arguments: query: Value error, "
        r"query must not be empty \(received str\)"
    )

    with pytest.raises(ValueError, match=empty_query_error):
        tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "   "}),
            workspace=Path("/tmp"),
        )


def test_websearch_tool_rejects_non_string_query() -> None:
    tool = WebSearchTool()
    query_type_error = (
        r"web_search invalid arguments: query: "
        r"Input should be a valid string \(received int\)"
    )

    with pytest.raises(ValueError, match=query_type_error):
        tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": 123}),
            workspace=Path("/tmp"),
        )


def test_websearch_tool_reports_missing_query_and_invalid_num_results() -> None:
    tool = WebSearchTool()
    missing_query_error = (
        r"web_search invalid arguments: query: "
        r"Input should be a valid string \(received NoneType\)"
    )
    invalid_num_results_error = (
        r"web_search invalid arguments: numResults: Value error, "
        r"numResults must be greater than or equal to 1 \(received int\)"
    )

    with pytest.raises(ValueError, match=missing_query_error):
        tool.invoke(ToolCall(tool_name="web_search", arguments={}), workspace=Path("/tmp"))

    with pytest.raises(ValueError, match=invalid_num_results_error):
        tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test", "numResults": 0}),
            workspace=Path("/tmp"),
        )


def test_websearch_tool_respects_num_results_limit() -> None:
    tool = WebSearchTool()

    fake_response = {
        "results": [{"title": "Example", "url": "https://example.com", "snippet": "snippet"}]
    }

    with (
        patch.dict("os.environ", {"EXA_API_KEY": "test-key"}, clear=False),
        patch(
            "httpx.Client.post",
            return_value=_json_response(fake_response),
        ) as post_mock,
    ):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test", "numResults": 5}),
            workspace=Path("/tmp"),
        )

    post_mock.assert_called_once()
    assert result.data["num_results"] == 5


def test_websearch_tool_defaults_to_8_results() -> None:
    tool = WebSearchTool()

    fake_response = {
        "results": [{"title": "Example", "url": "https://example.com", "snippet": "snippet"}]
    }

    with (
        patch.dict("os.environ", {"EXA_API_KEY": "test-key"}, clear=False),
        patch(
            "httpx.Client.post",
            return_value=_json_response(fake_response),
        ),
    ):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test"}),
            workspace=Path("/tmp"),
        )

    assert result.data["num_results"] == 8


def test_tools_package_and_default_registry_export_websearch_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "WebSearchTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("web_search").definition.name == "web_search"
    assert registry.resolve("web_search").definition.read_only is True
