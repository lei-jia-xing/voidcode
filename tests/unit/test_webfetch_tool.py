from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WebFetchTool


def test_webfetch_tool_rejects_invalid_url() -> None:
    tool = WebFetchTool()

    with pytest.raises(ValueError, match="http:// or https://"):
        tool.invoke(
            ToolCall(tool_name="web_fetch", arguments={"url": "ftp://example.com"}),
            workspace=Path("/tmp"),
        )


def test_webfetch_tool_rejects_non_string_url() -> None:
    tool = WebFetchTool()

    with pytest.raises(ValueError, match="string url"):
        tool.invoke(
            ToolCall(tool_name="web_fetch", arguments={"url": 123}),
            workspace=Path("/tmp"),
        )


def test_webfetch_tool_rejects_invalid_format() -> None:
    tool = WebFetchTool()

    with pytest.raises(ValueError, match="'text', 'markdown', or 'html'"):
        tool.invoke(
            ToolCall(
                tool_name="web_fetch", arguments={"url": "https://example.com", "format": "invalid"}
            ),
            workspace=Path("/tmp"),
        )


def test_tools_package_and_default_registry_export_webfetch_tool() -> None:
    registry = ToolRegistry.with_defaults()

    assert "WebFetchTool" in __import__("voidcode.tools", fromlist=["__all__"]).__all__
    assert registry.resolve("web_fetch").definition.name == "web_fetch"
    assert registry.resolve("web_fetch").definition.read_only is True
