from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WebFetchTool


class _StubResponse:
    def __init__(self, *, content: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._content = content
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._content


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


def test_webfetch_markdown_uses_markdown_conversion_for_html() -> None:
    tool = WebFetchTool()
    html = b"<html><body><h1>TITLE</h1><p>Hello</p></body></html>"
    with patch("urllib.request.urlopen", return_value=_StubResponse(content=html)):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://example.com", "format": "markdown"},
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert result.content is not None
    assert "# Title" in result.content or "TITLE" in result.content


def test_webfetch_tolerates_malformed_html() -> None:
    tool = WebFetchTool()
    malformed = b"<html><body>Hello <broken"
    with patch("urllib.request.urlopen", return_value=_StubResponse(content=malformed)):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch", arguments={"url": "https://example.com", "format": "text"}
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert isinstance(result.content, str)
    assert "Hello" in result.content


def test_webfetch_returns_attachment_for_image() -> None:
    tool = WebFetchTool()
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"fakepngdata"
    with patch(
        "urllib.request.urlopen",
        return_value=_StubResponse(content=image_bytes, content_type="image/png"),
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://example.com/image.png", "format": "markdown"},
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    attachment = result.data.get("attachment")
    assert isinstance(attachment, dict)
    assert attachment.get("mime") == "image/png"
    data_uri = attachment.get("data_uri")
    assert isinstance(data_uri, str)
    assert data_uri.startswith("data:image/png;base64,")
