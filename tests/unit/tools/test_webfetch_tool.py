from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import AttachmentOutput, TextOutput, ToolCall
from voidcode.tools.web_fetch import WebFetchTool


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


def test_webfetch_markdown_uses_markdown_conversion_for_html() -> None:
    tool = WebFetchTool()
    html = b"<html><body><h1>TITLE</h1><p>Hello</p></body></html>"
    with patch("httpx.Client.request", return_value=_response(content=html)):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://example.com", "format": "markdown"},
            ),
            context=ToolContext(workspace=Path("/tmp")),
        )

    assert isinstance(result.output, TextOutput)
    assert result.output.text == "# TITLE\n\nHello"


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
            context=ToolContext(workspace=Path("/tmp")),
        )

    assert isinstance(result.output, AttachmentOutput)
    assert result.output.mime == "image/png"
    assert result.output.data_uri.startswith("data:image/png;base64,")
    assert result.body is not None
    assert result.body.as_payload()["byte_count"] == len(image_bytes)


def test_webfetch_rejects_localhost_targets() -> None:
    tool = WebFetchTool()
    with pytest.raises(ValueError, match="blocked"):
        tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "http://127.0.0.1:8080", "format": "text"},
            ),
            context=ToolContext(workspace=Path("/tmp")),
        )


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
                context=ToolContext(workspace=Path("/tmp")),
            )


def test_webfetch_rejects_ipv4_mapped_ipv6_host() -> None:
    tool = WebFetchTool()

    with pytest.raises(ValueError, match="blocked"):
        tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "http://[::ffff:127.0.0.1]/", "format": "text"},
            ),
            context=ToolContext(workspace=Path("/tmp")),
        )
