from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from voidcode.tools import ToolCall
from voidcode.tools.code_search import CodeSearchTool


class _Resp:
    def __init__(self, text: str) -> None:
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._text.encode("utf-8")


def test_code_search_rejects_invalid_query_type() -> None:
    tool = CodeSearchTool()
    with pytest.raises(ValueError, match="string query"):
        tool.invoke(
            ToolCall(tool_name="code_search", arguments={"query": 123}), workspace=Path("/tmp")
        )


def test_code_search_uses_web_search_exa_and_parses_snippets() -> None:
    tool = CodeSearchTool()
    payload = {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": "```python\nfrom dataclasses import dataclass\n```",
                    "url": "https://example.com/a",
                },
                {
                    "type": "text",
                    "text": "```python\nfrom dataclasses import dataclass\n```",
                    "url": "https://example.com/a",
                },
                {
                    "type": "text",
                    "text": "```python\n@dataclass(slots=True)\nclass A: pass\n```",
                    "url": "https://example.com/b",
                },
            ]
        }
    }
    response_text = f"data: {json.dumps(payload)}\n"

    with patch("urllib.request.urlopen", return_value=_Resp(response_text)):
        result = tool.invoke(
            ToolCall(
                tool_name="code_search",
                arguments={"query": "python dataclass slots", "numResults": 3},
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert result.data["source"] == "exa_mcp_web_search_exa"
    assert result.data["snippet_count"] == 2
    assert len(result.data["sources"]) == 2


def test_code_search_falls_back_when_no_snippets() -> None:
    tool = CodeSearchTool()
    empty_payload = {"result": {"content": []}}
    response_text = f"data: {json.dumps(empty_payload)}\n"

    with patch("urllib.request.urlopen", return_value=_Resp(response_text)):
        result = tool.invoke(
            ToolCall(tool_name="code_search", arguments={"query": "python context managers"}),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert result.data["source"] == "duckduckgo_fallback"
    assert result.data["snippet_count"] == 0


def test_code_search_handles_mcp_error_object() -> None:
    tool = CodeSearchTool()
    payload = {"error": {"message": "rate limit"}}
    response_text = f"data: {json.dumps(payload)}\n"

    with patch("urllib.request.urlopen", return_value=_Resp(response_text)):
        with pytest.raises(ValueError, match="rate limit"):
            tool.invoke(
                ToolCall(tool_name="code_search", arguments={"query": "python asyncio"}),
                workspace=Path("/tmp"),
            )


def test_code_search_fallback_on_url_error() -> None:
    tool = CodeSearchTool()

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        result = tool.invoke(
            ToolCall(tool_name="code_search", arguments={"query": "python dataclass"}),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert result.data["source"] == "duckduckgo_fallback"
    assert "fallback_reason" in result.data
