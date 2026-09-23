from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from ..security.url_policy import validate_redirect_target, validate_url
from .contracts import ToolCall, ToolDefinition, ToolResult

MAX_RESPONSE_SIZE = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30

logger = logging.getLogger(__name__)

# Non-content tags removed before any text or markdown extraction. This tool is a
# fetcher, not a readability extractor: no boilerplate/nav heuristics live here.
REMOVED_TAGS = ("script", "style", "noscript", "iframe", "object", "embed")

# `<pre class="language-python">` / `<pre><code class="language-python">` (highlight.js,
# Prism and GitHub all use the `language-` prefix; some generators use `lang-`).
CODE_LANGUAGE_CLASS_PATTERN = re.compile(r"^(?:language|lang)-(.+)$")


def _clean_html(html: str) -> BeautifulSoup:
    """Parse HTML and drop non-content tags so neither extraction path sees markup noise."""
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in REMOVED_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    return soup


def _extract_text_from_html(html: str) -> str:
    soup = _clean_html(html)

    for tag in soup.find_all(["br", "li", "p", "div", "section", "article", "tr"]):
        tag.append("\n")

    result = soup.get_text(separator=" ")
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r" *\n *", "\n", result)
    return result.strip()


def _code_language(pre_element: Tag) -> str | None:
    """Read a code language from `class="language-x"`/`class="lang-x"` on `<pre>` or its `<code>`."""
    for element in (pre_element, pre_element.find("code")):
        if not isinstance(element, Tag):
            continue
        class_attribute = element.get("class")
        if not isinstance(class_attribute, list):
            continue
        for class_name in class_attribute:
            match = CODE_LANGUAGE_CLASS_PATTERN.match(str(class_name))
            if match is not None:
                return match.group(1)
    return None


def _html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown with markdownify, then normalize whitespace only.

    Structure (heading level, link target, code fence language, table cells) is owned by
    markdownify; the regexes below only touch blank lines and trailing whitespace.

    Options:
    - `heading_style="ATX"`: `#`-prefixed headings instead of setext underlines, so levels
      survive re-parsing and nested headings are unambiguous.
    - `newline_style="backslash"`: `<br>` becomes a backslash hard break, which survives the
      per-line trailing-whitespace strip below (the default `spaces` style would be erased).
    - `code_language_callback`: preserves `language-x`/`lang-x` classes as the fence info string.
    Everything else keeps markdownify 1.2.3 defaults, including GFM pipe tables
    (`table_infer_header=False` leaves the header row empty rather than promoting a data row).
    """
    converter = MarkdownConverter(
        heading_style="ATX",
        newline_style="backslash",
        code_language_callback=_code_language,
    )
    markdown = converter.convert_soup(_clean_html(html))
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = re.sub(r"[ \t]+$", "", markdown, flags=re.MULTILINE)
    return markdown.strip()


def _html_to_markdown_or_text(html: str, *, url: str) -> str:
    """Depth guard for the single markdown pipeline.

    markdownify converts recursively, so a hostile or merely broken page with hundreds of
    nested block elements exhausts the interpreter stack (measured: 494 nested `<div>` work,
    495 raise with the default recursion limit). Degrade that one case to the plain-text
    extraction that `format="text"` already uses instead of failing the fetch. This is a
    documented depth guard, not a second converter and not a resurrection of the removed
    line heuristics; every other failure still propagates.
    """
    try:
        return _html_to_markdown(html)
    except RecursionError:
        logger.warning("web_fetch markdown conversion exceeded the nesting depth guard for %s; returning plain text", url)
        return _extract_text_from_html(html)


class WebFetchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="web_fetch",
        description="Fetch content from a URL. Supports text, markdown, and HTML formats.",
        input_schema={
            "url": {"type": "string", "description": "The URL to fetch content from"},
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "description": "Output format: text, markdown, or html",
            },
            "timeout": {"type": "integer", "description": "Timeout in seconds (max 120)"},
            "required": ["url"],
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        return self._invoke(call, workspace=workspace, runtime_timeout_seconds=None)

    def invoke_with_runtime_timeout(self, call: ToolCall, *, workspace: Path, timeout_seconds: int) -> ToolResult:
        return self._invoke(call, workspace=workspace, runtime_timeout_seconds=timeout_seconds)

    def _invoke(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        runtime_timeout_seconds: int | None,
    ) -> ToolResult:
        _ = workspace
        url_value = call.arguments.get("url")
        if not isinstance(url_value, str):
            raise ValueError("web_fetch requires a string url argument")

        _ = validate_url(url_value)

        format_value = call.arguments.get("format", "markdown")
        if not isinstance(format_value, str) or format_value not in ("text", "markdown", "html"):
            raise ValueError("web_fetch format must be 'text', 'markdown', or 'html'")

        timeout_value = call.arguments.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout_value, (int, float)) and timeout_value > 0:
            timeout = min(int(timeout_value), 120)
        else:
            timeout = DEFAULT_TIMEOUT
        if runtime_timeout_seconds is not None:
            timeout = min(timeout, runtime_timeout_seconds)

        content: str = ""
        data: bytes = b""
        mime: str = ""

        accept_by_format = {
            "markdown": ("text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"),
            "text": ("text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1"),
            "html": ("text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1"),
        }

        accept_header = accept_by_format[format_value]

        # Use a more realistic User-Agent to avoid bot detection on some servers
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) VoidCode/1.0 Chrome/110.0.5481.100 Safari/537.36"

        headers = {
            "User-Agent": ua,
            "Accept": accept_header,
        }

        current_url = url_value
        max_redirects = 5

        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                for _ in range(max_redirects + 1):
                    response = client.request("GET", current_url, headers=headers)

                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("Failed to fetch URL: redirect response missing location")
                        redirected = validate_redirect_target(base_url=current_url, location=location)
                        current_url = redirected.url
                        continue

                    if response.status_code >= 400:
                        raise ValueError(f"HTTP error {response.status_code}: {response.reason_phrase}")

                    final_url = str(response.url)
                    _ = validate_url(final_url)
                    content_type = response.headers.get("Content-Type", "")
                    content_length = response.headers.get("Content-Length")

                    if content_length:
                        try:
                            parsed_length = int(content_length)
                        except TypeError, ValueError:
                            parsed_length = None
                        if parsed_length is not None and parsed_length > MAX_RESPONSE_SIZE:
                            limit_mb = MAX_RESPONSE_SIZE // 1024 // 1024
                            raise ValueError(f"Response too large (exceeds {limit_mb}MB limit)")

                    total = 0
                    chunks: list[bytes] = []
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_SIZE:
                            limit_mb = MAX_RESPONSE_SIZE // 1024 // 1024
                            raise ValueError(f"Response too large (exceeds {limit_mb}MB limit)")
                        chunks.append(chunk)

                    data = b"".join(chunks)
                    content = data.decode("utf-8", errors="replace")
                    mime = content_type.split(";")[0].strip().lower() if content_type else ""
                    break
                else:
                    raise ValueError("Failed to fetch URL: too many redirects")
        except httpx.HTTPError as exc:
            raise ValueError(f"Failed to fetch URL: {exc}") from exc

        # Normal post-fetch processing (outside of except blocks)
        if format_value == "html":
            output = content
        elif format_value == "text":
            output = _extract_text_from_html(content)
        elif format_value == "markdown":
            if mime and mime.startswith("image/"):
                b64 = base64.b64encode(data).decode("ascii")
                data_uri = f"data:{mime};base64,{b64}"
                return ToolResult(
                    tool_name=self.definition.name,
                    status="ok",
                    content="",
                    data={
                        "url": url_value,
                        "content_type": mime,
                        "format": format_value,
                        "byte_count": len(data),
                        "timeout_seconds": timeout,
                        "attachment": {"mime": mime, "data_uri": data_uri},
                    },
                    truncated=False,
                    partial=False,
                    timeout_seconds=timeout,
                )
            if "text/html" in mime:
                output = _html_to_markdown_or_text(content, url=url_value)
            else:
                output = content

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Fetched {len(output)} characters from {url_value} as {format_value}.",
            data={
                "url": url_value,
                "content_type": mime,
                "format": format_value,
                "byte_count": len(data),
                "timeout_seconds": timeout,
                "content": output,
            },
            truncated=False,
            partial=False,
            timeout_seconds=timeout,
        )
