from __future__ import annotations

import base64
import logging
import re
import urllib.parse
from dataclasses import dataclass, replace
from typing import final

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    AttachmentOutput,
    OutputBounds,
    OutputReference,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolSuccess,
)

from .artifacts import (
    MAX_INLINE_ATTACHMENT_BYTES,
    RASTER_IMAGE_MIMES,
    artifact_uri,
    create_artifact,
)

MAX_RESPONSE_SIZE = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30

#: Inline text bound for one fetched document. The full response bytes are spilled via the
#: service spill method when the handler exposes it; this is what a single result carries
#: inline without risking the host's frame limit.
MAX_INLINE_TEXT_BYTES = 50 * 1024

logger = logging.getLogger(__name__)


def _bounded_inline_text(text: str) -> tuple[str, bool]:
    """Cut inline text at ``MAX_INLINE_TEXT_BYTES`` without splitting a UTF-8 character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_INLINE_TEXT_BYTES:
        return text, False
    return encoded[:MAX_INLINE_TEXT_BYTES].decode("utf-8", errors="ignore"), True


@dataclass(frozen=True, slots=True)
class WebFetchResultBody:
    original_url: str
    final_url: str
    content_type: str
    mime: str
    format: str
    byte_count: int
    timeout_seconds: int
    used_encoding: str | None
    provenance: str
    source_artifact: dict[str, object]

    def as_payload(self) -> dict[str, object]:
        return {
            "original_url": self.original_url,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "mime": self.mime,
            "format": self.format,
            "byte_count": self.byte_count,
            "timeout_seconds": self.timeout_seconds,
            "used_encoding": self.used_encoding,
            "provenance": self.provenance,
            "source_artifact": dict(self.source_artifact),
        }


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


@final
class WebFetchTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        _ = context
        return self._invoke(call, runtime_timeout_seconds=None, services=services)

    def invoke_with_runtime_timeout(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
        timeout_seconds: int,
    ) -> ToolResult:
        _ = context
        return self._invoke(call, runtime_timeout_seconds=timeout_seconds, services=services)

    def _invoke(
        self,
        call: ToolCall,
        *,
        runtime_timeout_seconds: int | None,
        services: PluginServices,
    ) -> ToolResult:
        original_url = call.arguments.get("url")
        if not isinstance(original_url, str):
            raise ValueError("web_fetch requires a string url argument")

        format_value = call.arguments.get("format", "markdown")
        if not isinstance(format_value, str) or format_value not in ("text", "markdown", "html"):
            raise ValueError("web_fetch format must be 'text', 'markdown', or 'html'")

        timeout_value = call.arguments.get("timeout", DEFAULT_TIMEOUT)
        timeout = min(int(timeout_value), 120) if isinstance(timeout_value, (int, float)) and timeout_value > 0 else DEFAULT_TIMEOUT
        if runtime_timeout_seconds is not None:
            timeout = min(timeout, runtime_timeout_seconds)

        accept_by_format = {
            "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
            "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
            "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) VoidCode/1.0 Chrome/110 Safari/537.36",
            "Accept": accept_by_format[format_value],
        }

        current_url = original_url
        max_redirects = 5
        data = b""
        content = ""
        content_type = ""
        mime = ""
        final_url = original_url
        used_encoding: str | None = None
        is_attachment = False

        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                for _ in range(max_redirects + 1):
                    with client.stream("GET", current_url, headers=headers) as response:
                        if response.is_redirect:
                            location = response.headers.get("Location")
                            if not location:
                                raise ValueError("Failed to fetch URL: redirect response missing location")
                            current_url = urllib.parse.urljoin(current_url, location)
                            continue

                        if response.status_code >= 400:
                            raise ValueError(f"HTTP error {response.status_code}: {response.reason_phrase}")

                        final_url = str(response.url)
                        content_type = response.headers.get("Content-Type", "")
                        mime = content_type.split(";", 1)[0].strip().lower() if content_type else ""
                        is_attachment = format_value == "markdown" and mime in RASTER_IMAGE_MIMES
                        is_textual = (
                            mime.startswith("text/")
                            or mime in {"application/json", "application/xml", "application/javascript"}
                            or mime.endswith(("+json", "+xml"))
                        )
                        content_length = response.headers.get("Content-Length")
                        if content_length is not None:
                            try:
                                parsed_length = int(content_length)
                            except ValueError:
                                parsed_length = None
                            if parsed_length is not None and parsed_length > MAX_RESPONSE_SIZE:
                                raise ValueError(f"Response too large (exceeds {MAX_RESPONSE_SIZE // 1024 // 1024}MB limit)")

                        total = 0
                        chunks: list[bytes] = []
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_RESPONSE_SIZE:
                                raise ValueError(f"Response too large (exceeds {MAX_RESPONSE_SIZE // 1024 // 1024}MB limit)")
                            chunks.append(chunk)
                        data = b"".join(chunks)

                        if is_textual:
                            try:
                                used_encoding = response.encoding or "utf-8"
                                content = data.decode(used_encoding)
                            except (LookupError, UnicodeDecodeError) as exc:
                                raise ValueError(f"Failed to decode response with declared content type {content_type!r}: {exc}") from exc
                        break
                else:
                    raise ValueError("Failed to fetch URL: too many redirects")
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ValueError(f"Failed to fetch URL: {exc}") from exc

        source_artifact = create_artifact(
            services,
            kind="web_fetch_response",
            content=data,
            mime=mime or "application/octet-stream",
            source=final_url,
        )
        reference_uri = artifact_uri(source_artifact)
        if source_artifact is not None and reference_uri is not None:
            bounds = OutputBounds(
                reference=OutputReference(uri=reference_uri, artifact=source_artifact),
                source="web_fetch",
            )
        else:
            # Without a spill method the raw response is not spilled, so the reference
            # is omitted and the bounded inline text is the only copy.
            bounds = OutputBounds(source="web_fetch")
        raw_reference = reference_uri or "unavailable"
        body = WebFetchResultBody(
            original_url=original_url,
            final_url=final_url,
            content_type=content_type,
            mime=mime,
            format=format_value,
            byte_count=len(data),
            timeout_seconds=timeout,
            used_encoding=used_encoding,
            provenance="web_fetch",
            source_artifact={} if source_artifact is None else dict(source_artifact),
        )

        provenance = (
            f"Source URL: {original_url} | Final URL: {final_url} | "
            f"MIME: {mime or 'application/octet-stream'} | charset: {used_encoding or 'unspecified'} | raw: {raw_reference}"
        )
        if is_attachment:
            data_uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}" if len(data) <= MAX_INLINE_ATTACHMENT_BYTES else None
            presentation = f"{provenance}\nFetched {mime} attachment."
            if data_uri is None:
                presentation += f" Inline bytes omitted above {MAX_INLINE_ATTACHMENT_BYTES} bytes; raw: {raw_reference}"
            return ToolSuccess(
                tool_name=self.definition.name,
                output=AttachmentOutput(
                    mime=mime,
                    data_uri=data_uri,
                    presentation=presentation,
                    bounds=bounds,
                ),
                body=body,
            )

        if not is_textual:
            output = "Opaque response bytes were preserved without text decoding."
        elif format_value == "html":
            output = content
        elif format_value == "text":
            output = _extract_text_from_html(content)
        elif mime in {"text/html", "application/xhtml+xml"}:
            output = _html_to_markdown_or_text(content, url=final_url)
        else:
            output = content

        output, inline_truncated = _bounded_inline_text(output)
        if inline_truncated:
            provenance += f" | truncated to {MAX_INLINE_TEXT_BYTES} bytes inline; raw: {raw_reference}"

        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(
                f"{provenance}\n\n{output}",
                presentation=f"Fetched {len(output)} characters from {final_url} as {format_value}.",
                bounds=replace(bounds, truncated=True) if inline_truncated else bounds,
            ),
            body=body,
        )
