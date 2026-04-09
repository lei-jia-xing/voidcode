from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WebSearchTool


def test_websearch_tool_rejects_empty_query() -> None:
    tool = WebSearchTool()

    with pytest.raises(ValueError, match="must not be empty"):
        tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "   "}),
            workspace=Path("/tmp"),
        )


def test_websearch_tool_rejects_non_string_query() -> None:
    tool = WebSearchTool()

    with pytest.raises(ValueError, match="string query"):
        tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": 123}),
            workspace=Path("/tmp"),
        )


def test_websearch_tool_respects_num_results_limit() -> None:
    tool = WebSearchTool()

    fake_response = {
        "results": [{"title": "Example", "url": "https://example.com", "snippet": "snippet"}]
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(fake_response).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=_Resp()):
        result = tool.invoke(
            ToolCall(tool_name="web_search", arguments={"query": "test", "numResults": 5}),
            workspace=Path("/tmp"),
        )

    assert result.data["num_results"] == 5


def test_websearch_tool_defaults_to_8_results() -> None:
    tool = WebSearchTool()

    fake_response = {
        "results": [{"title": "Example", "url": "https://example.com", "snippet": "snippet"}]
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(fake_response).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=_Resp()):
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
