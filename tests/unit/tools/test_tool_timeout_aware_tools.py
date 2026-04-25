from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx

from voidcode.tools import AstGrepSearchTool, CodeSearchTool, ToolCall, WebFetchTool, WebSearchTool


def test_ast_grep_search_honors_runtime_timeout(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    _ = sample.write_text("print('hello')\n", encoding="utf-8")
    tool = AstGrepSearchTool()

    with patch("subprocess.run", side_effect=TimeoutError("boom")):
        result = tool.invoke_with_runtime_timeout(
            ToolCall(
                tool_name="ast_grep_search", arguments={"pattern": "print($X)", "path": "sample.py"}
            ),
            workspace=tmp_path,
            timeout_seconds=1,
        )

    assert result.status == "error"


def test_webfetch_honors_runtime_timeout(tmp_path: Path) -> None:
    tool = WebFetchTool()
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/html"},
        content=b"<html><body>ok</body></html>",
        request=httpx.Request("GET", "https://example.com"),
    )

    with patch("httpx.Client.request", return_value=response):
        result = tool.invoke_with_runtime_timeout(
            ToolCall(
                tool_name="web_fetch", arguments={"url": "https://example.com", "format": "text"}
            ),
            workspace=tmp_path,
            timeout_seconds=1,
        )

    assert result.status == "ok"


def test_websearch_honors_runtime_timeout(tmp_path: Path) -> None:
    tool = WebSearchTool()
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.side_effect = OSError("offline")
        result = tool.invoke_with_runtime_timeout(
            ToolCall(tool_name="web_search", arguments={"query": "test"}),
            workspace=tmp_path,
            timeout_seconds=1,
        )

    assert result.status == "ok"


def test_codesearch_honors_runtime_timeout(tmp_path: Path) -> None:
    tool = CodeSearchTool()
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.side_effect = OSError("offline")
        result = tool.invoke_with_runtime_timeout(
            ToolCall(tool_name="code_search", arguments={"query": "test"}),
            workspace=tmp_path,
            timeout_seconds=1,
        )

    assert result.status == "ok"
