from __future__ import annotations

import logging
from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx
import pytest

from voidcode.runtime.service import ToolRegistry
from voidcode.tools import ToolCall, WebFetchTool
from voidcode.tools.web_fetch import _extract_text_from_html, _html_to_markdown


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
            ToolCall(tool_name="web_fetch", arguments={"url": "https://example.com", "format": "invalid"}),
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
    assert result.data["content"] == "# TITLE\n\nHello"


def test_webfetch_markdown_end_to_end_preserves_document_structure() -> None:
    tool = WebFetchTool()
    html = (
        b"<html><body>\n"
        b"<h1>Guide</h1>\n"
        b'<p>See <a href="https://example.com/docs">docs</a>.</p>\n'
        b'<pre><code class="language-python">x = 1</code></pre>\n'
        b"<p>THIS IS IMPORTANT</p>\n"
        b"</body></html>"
    )
    with patch("httpx.Client.request", return_value=_response(content=html)):
        result = tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://example.com", "format": "markdown"},
            ),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert result.data["content"] == ("# Guide\n\nSee [docs](https://example.com/docs).\n\n```python\nx = 1\n```\n\nTHIS IS IMPORTANT")


def test_webfetch_markdown_degrades_to_plain_text_on_pathological_nesting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool = WebFetchTool()
    nested = "<div>" * 2000 + "deep content" + "</div>" * 2000
    html = f"<html><body>{nested}</body></html>".encode()
    with caplog.at_level(logging.WARNING, logger="voidcode.tools.web_fetch"):
        with patch("httpx.Client.request", return_value=_response(content=html)):
            result = tool.invoke(
                ToolCall(
                    tool_name="web_fetch",
                    arguments={"url": "https://example.com/deep", "format": "markdown"},
                ),
                workspace=Path("/tmp"),
            )

    assert result.status == "ok"
    assert result.data["content"] == _extract_text_from_html(html.decode())
    assert "deep content" in str(result.data["content"])
    guard_warnings = [record for record in caplog.records if record.name == "voidcode.tools.web_fetch"]
    assert len(guard_warnings) == 1
    assert "https://example.com/deep" in guard_warnings[0].getMessage()


def test_webfetch_markdown_happy_path_does_not_trigger_depth_guard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool = WebFetchTool()
    html = b"<html><body><h1>Title</h1><p>Body</p></body></html>"
    with caplog.at_level(logging.WARNING, logger="voidcode.tools.web_fetch"):
        with patch("httpx.Client.request", return_value=_response(content=html)):
            result = tool.invoke(
                ToolCall(
                    tool_name="web_fetch",
                    arguments={"url": "https://example.com", "format": "markdown"},
                ),
                workspace=Path("/tmp"),
            )

    assert result.data["content"] == "# Title\n\nBody"
    assert [record for record in caplog.records if record.name == "voidcode.tools.web_fetch"] == []


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("h1", "# Section"), ("h2", "## Section"), ("h3", "### Section"), ("h4", "#### Section")],
)
def test_html_to_markdown_preserves_heading_levels(tag: str, expected: str) -> None:
    assert _html_to_markdown(f"<{tag}>Section</{tag}>") == expected


def test_html_to_markdown_preserves_link_targets() -> None:
    html = '<p>See <a href="https://example.com/docs">the docs</a>.</p>'

    assert _html_to_markdown(html) == "See [the docs](https://example.com/docs)."


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            '<pre><code class="language-python">def add(a, b):\n    return a + b\n</code></pre>',
            "```python\ndef add(a, b):\n    return a + b\n```",
        ),
        ('<pre class="lang-rust"><code>fn main() {}</code></pre>', "```rust\nfn main() {}\n```"),
    ],
)
def test_html_to_markdown_emits_fenced_code_block_with_language(html: str, expected: str) -> None:
    assert _html_to_markdown(html) == expected


def test_html_to_markdown_emits_table_rows_and_cells() -> None:
    html = "<table><thead><tr><th>Name</th><th>Type</th></tr></thead><tbody><tr><td>id</td><td>int</td></tr></tbody></table>"

    assert _html_to_markdown(html) == "| Name | Type |\n| --- | --- |\n| id | int |"


def test_html_to_markdown_keeps_headerless_table_rows_as_data() -> None:
    html = "<table><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>"

    assert _html_to_markdown(html) == "|  |  |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"


def test_html_to_markdown_emits_nested_and_ordered_lists() -> None:
    html = "<ul><li>alpha<ul><li>alpha one</li></ul></li><li>beta</li></ul><ol><li>first</li><li>second</li></ol>"

    assert _html_to_markdown(html) == "* alpha\n  + alpha one\n* beta\n\n1. first\n2. second"


def test_html_to_markdown_preserves_image_alt_text() -> None:
    html = '<p><img src="/logo.png" alt="Project logo"></p>'

    assert _html_to_markdown(html) == "![Project logo](/logo.png)"


def test_html_to_markdown_decodes_html_entities() -> None:
    assert _html_to_markdown("<p>Tom &amp; Jerry</p>") == "Tom & Jerry"


def test_html_to_markdown_keeps_all_caps_paragraph_as_paragraph() -> None:
    assert _html_to_markdown("<p>THIS IS AN ALL CAPS PARAGRAPH</p>") == "THIS IS AN ALL CAPS PARAGRAPH"


def test_html_to_markdown_keeps_line_breaks_from_br() -> None:
    assert _html_to_markdown("<p>line one<br>line two</p>") == "line one\\\nline two"


def test_html_to_markdown_drops_non_content_tags() -> None:
    html = "<div>keep<script>var x = 1;</script><style>p { color: red; }</style></div>"

    assert _html_to_markdown(html) == "keep"


def test_html_to_markdown_collapses_excess_blank_lines() -> None:
    html = "<p>one</p>" + "<div></div>" * 5 + "<p>two</p>"

    assert _html_to_markdown(html) == "one\n\ntwo"


def test_webfetch_tolerates_malformed_html() -> None:
    tool = WebFetchTool()
    malformed = b"<html><body>Hello <broken"
    with patch("httpx.Client.request", return_value=_response(content=malformed)):
        result = tool.invoke(
            ToolCall(tool_name="web_fetch", arguments={"url": "https://example.com", "format": "text"}),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert isinstance(result.content, str)
    assert "Hello" in str(result.data["content"])


def test_webfetch_text_preserves_list_item_separation() -> None:
    tool = WebFetchTool()
    html = b"<html><body><ul><li>Alpha</li><li>Beta</li></ul></body></html>"
    with patch("httpx.Client.request", return_value=_response(content=html)):
        result = tool.invoke(
            ToolCall(tool_name="web_fetch", arguments={"url": "https://example.com", "format": "text"}),
            workspace=Path("/tmp"),
        )

    assert result.status == "ok"
    assert isinstance(result.content, str)
    fetched = str(result.data["content"])
    assert "AlphaBeta" not in fetched
    assert "Alpha" in fetched
    assert "Beta" in fetched


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
    assert not hasattr(result, "attachment")


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


def test_webfetch_rejects_redirect_chain_to_metadata_host() -> None:
    tool = WebFetchTool()
    first = httpx.Response(
        302,
        headers={"Location": "https://metadata.google.internal/computeMetadata/v1"},
        request=httpx.Request("GET", "https://example.com/start"),
    )

    with patch("httpx.Client.request", side_effect=[first]):
        with pytest.raises(ValueError, match="blocked"):
            tool.invoke(
                ToolCall(
                    tool_name="web_fetch",
                    arguments={"url": "https://example.com/start", "format": "text"},
                ),
                workspace=Path("/tmp"),
            )


def test_webfetch_rejects_ipv4_mapped_ipv6_host() -> None:
    tool = WebFetchTool()

    with pytest.raises(ValueError, match="blocked"):
        tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "http://[::ffff:127.0.0.1]/", "format": "text"},
            ),
            workspace=Path("/tmp"),
        )


def test_webfetch_rejects_url_credentials() -> None:
    tool = WebFetchTool()

    with pytest.raises(ValueError, match="must not include credentials"):
        tool.invoke(
            ToolCall(
                tool_name="web_fetch",
                arguments={"url": "https://user:pass@example.com/secret", "format": "text"},
            ),
            workspace=Path("/tmp"),
        )


def test_webfetch_fails_when_redirect_location_is_missing() -> None:
    tool = WebFetchTool()
    redirect = httpx.Response(
        302,
        headers={},
        request=httpx.Request("GET", "https://example.com/start"),
    )

    with patch("httpx.Client.request", side_effect=[redirect]):
        with pytest.raises(ValueError, match="missing location"):
            tool.invoke(
                ToolCall(
                    tool_name="web_fetch",
                    arguments={"url": "https://example.com/start", "format": "text"},
                ),
                workspace=Path("/tmp"),
            )
