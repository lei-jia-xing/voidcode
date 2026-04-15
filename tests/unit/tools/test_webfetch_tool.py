from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx
import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WebFetchTool


def _response(
    *,
    content: bytes,
    content_type: str = "text/html; charset=utf-8",
    final_url: str = "https://example.com",
    content_length: str | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "Content-Type": content_type,
            "Content-Length": content_length if content_length is not None else str(len(content)),
        },
        content=content,
        request=httpx.Request("GET", final_url),
    )


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
    with patch("httpx.Client.request", return_value=_response(content=html)) as request_mock:
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://example.com", "format": "markdown"},
            ),
            workspace=Path("/tmp"),
        )

    request_mock.assert_called_once()
    assert result.status == "ok"
    assert result.content is not None
    assert "# Title" in result.content or "TITLE" in result.content


def test_webfetch_tolerates_malformed_html() -> None:
    tool = WebFetchTool()
    malformed = b"<html><body>Hello <broken"
    with patch("httpx.Client.request", return_value=_response(content=malformed)):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch", arguments={"url": "https://example.com", "format": "text"}
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert isinstance(result.content, str)
    assert "Hello" in result.content


def test_webfetch_text_preserves_list_item_separation() -> None:
    tool = WebFetchTool()
    html = b"<html><body><ul><li>Alpha</li><li>Beta</li></ul></body></html>"
    with patch("httpx.Client.request", return_value=_response(content=html)):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch", arguments={"url": "https://example.com", "format": "text"}
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert isinstance(result.content, str)
    assert "AlphaBeta" not in result.content
    assert "Alpha" in result.content
    assert "Beta" in result.content


def test_webfetch_returns_attachment_for_image() -> None:
    tool = WebFetchTool()
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"fakepngdata"
    with patch(
        "httpx.Client.request",
        return_value=_response(content=image_bytes, content_type="image/png"),
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://example.com/image.png", "format": "markdown"},
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    attachment_raw = result.data.get("attachment")
    assert isinstance(attachment_raw, dict)
    attachment = cast(dict[str, object], attachment_raw)
    assert attachment.get("mime") == "image/png"
    data_uri = attachment.get("data_uri")
    assert isinstance(data_uri, str)
    assert data_uri.startswith("data:image/png;base64,")


def test_webfetch_rejects_localhost_targets() -> None:
    tool = WebFetchTool()
    with pytest.raises(ValueError, match="blocked"):
        tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "http://127.0.0.1:8080", "format": "text"},
            ),
            workspace=Path("/tmp"),
        )


def test_webfetch_tolerates_invalid_content_length_header() -> None:
    tool = WebFetchTool()
    html = b"<html><body>ok</body></html>"
    with patch(
        "httpx.Client.request",
        return_value=_response(content=html, content_type="text/html", content_length="abc"),
    ):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://example.com", "format": "markdown"},
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert isinstance(result.content, str)


def test_webfetch_rejects_redirect_to_localhost() -> None:
    tool = WebFetchTool()

    with patch(
        "httpx.Client.request",
        return_value=_response(content=b"ok", final_url="http://127.0.0.1:8080/internal"),
    ):
        with pytest.raises(ValueError, match="blocked"):
            tool.invoke(
                ToolCall(
                    tool_name="web_fetch",
                    arguments={"url": "https://example.com", "format": "text"},
                ),
                workspace=Path("/tmp"),
            )
