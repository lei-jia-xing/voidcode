from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Literal, cast, final

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter
from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.limits import TOOL_OUTPUT_BYTES
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    AttachmentOutput,
    OutputBounds,
    OutputReference,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .args import parse_tool_args
from .artifacts import (
    ARTIFACT_ID_PATTERN,
    MAX_ARTIFACT_PAGE_BYTES,
    MAX_INLINE_ATTACHMENT_BYTES,
    RASTER_IMAGE_MIMES,
    artifact_uri,
    create_artifact,
    read_artifact_page,
)

MAX_RESPONSE_SIZE = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30

#: A continuation cursor addresses the immutable manifest spilled next to the rendered bytes:
#: never the rendered page itself, and never a caller-chosen artifact digest.
CONTENT_REF_PREFIX = "voidcode://artifact/"
CONTINUATION_KIND = "web_fetch_continuation"
CONTINUATION_VERSION = 1
#: Manifests are a tiny metadata envelope, so one bounded read resolves a cursor.
MAX_MANIFEST_BYTES = 8 * 1024
#: Page shrink floor when long URLs make the body overhead crowd the model projection.
MIN_PAGE_BYTES = 1024
RENDERED_MIME_BY_FORMAT = {
    "text": "text/plain; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}

logger = logging.getLogger(__name__)


class WebFetchArgs(BaseModel):
    """Initial fetch branch: a known URL and its presentation options."""

    model_config = ConfigDict(extra="forbid", strict=True)

    url: str
    format: Literal["text", "markdown", "html"] = "markdown"
    timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1)


class WebFetchContinuationArgs(BaseModel):
    """Continuation branch: one cursor from a previous result, never a URL."""

    model_config = ConfigDict(extra="forbid", strict=True)

    content_ref: str
    byte_offset: int = Field(ge=0)


class _FetchFailure(Exception):
    """One safe, actionable fetch cause; it never carries a raw exception string."""


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
    #: The continuation manifest; its ``uri`` is the ``content_ref`` a caller passes back.
    content_artifact: dict[str, object]
    content_ref: str | None
    byte_offset: int
    next_byte_offset: int | None
    total_bytes: int
    eof: bool
    truncated: bool

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
            "content_artifact": dict(self.content_artifact),
            "content_ref": self.content_ref,
            "byte_offset": self.byte_offset,
            "next_byte_offset": self.next_byte_offset,
            "total_bytes": self.total_bytes,
            "eof": self.eof,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class WebFetchContinuationBody:
    content_ref: str
    format: str
    original_url: str
    final_url: str
    byte_offset: int
    next_byte_offset: int | None
    total_bytes: int
    eof: bool
    truncated: bool
    provenance: str

    def as_payload(self) -> dict[str, object]:
        return {
            "content_ref": self.content_ref,
            "format": self.format,
            "original_url": self.original_url,
            "final_url": self.final_url,
            "byte_offset": self.byte_offset,
            "next_byte_offset": self.next_byte_offset,
            "total_bytes": self.total_bytes,
            "eof": self.eof,
            "truncated": self.truncated,
            "provenance": self.provenance,
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
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="web_fetch",
        description=(
            "Fetch one URL as text, markdown, or HTML. When a textual result is truncated it returns content_ref and next_byte_offset; "
            "call again with exactly those two fields to read the next page from this session's artifact store, without fetching the network again."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Initial call: the URL to fetch content from"},
                "format": {"type": "string", "enum": ["text", "markdown", "html"], "description": "Initial call: output format (default: markdown)"},
                "timeout": {"type": "integer", "description": "Initial call: timeout in seconds (max 120)"},
                "content_ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Continuation call: the content_ref from a truncated web_fetch result",
                },
                "byte_offset": {"type": "integer", "minimum": 0, "description": "Continuation call: the next_byte_offset from the previous page"},
            },
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "format": {"type": "string", "enum": ["text", "markdown", "html"]},
                        "timeout": {"type": "integer"},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"content_ref": {"type": "string", "minLength": 1}, "byte_offset": {"type": "integer", "minimum": 0}},
                    "required": ["content_ref", "byte_offset"],
                    "additionalProperties": False,
                },
            ],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.NETWORK}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:  # noqa: ARG002 - required by the frozen Tool ABI
        arguments = call.arguments
        try:
            # Branch on raw keys before any model validation: the runtime schema gate
            # enforces the oneOf, and this is the same split for direct invocation.
            if "content_ref" in arguments or "byte_offset" in arguments:
                return _continuation_result(
                    self.definition,
                    parse_tool_args(WebFetchContinuationArgs, arguments, tool_name=self.definition.name),
                    services=services,
                )
            return _initial_result(
                self.definition,
                parse_tool_args(WebFetchArgs, arguments, tool_name=self.definition.name),
                services=services,
            )
        except _FetchFailure as failure:
            return ToolFailure(tool_name=self.definition.name, error=str(failure))

        raise AssertionError("unreachable: invoke dispatches to _initial_result or _continuation_result")


@dataclass(frozen=True, slots=True)
class _Fetched:
    original_url: str
    final_url: str
    content_type: str
    mime: str
    data: bytes
    content: str
    used_encoding: str | None
    is_textual: bool
    is_attachment: bool


_TEXTUAL_MIMES = frozenset({"application/json", "application/xml", "application/javascript"})


def _is_textual(mime: str) -> bool:
    return mime.startswith("text/") or mime in _TEXTUAL_MIMES or mime.endswith(("+json", "+xml"))


def _too_large_message(url: str) -> str:
    return f"web_fetch failed: {url} exceeds the {MAX_RESPONSE_SIZE // 1024 // 1024} MiB response limit. Fetch a smaller page or a more specific URL."


def _safe_reason(phrase: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", phrase).strip()[:120] or "no reason phrase"


def _perform_fetch(*, original_url: str, format_value: str, timeout: int) -> _Fetched:
    """Fetch one document. Raises ``_FetchFailure`` with safe, actionable cause text."""
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
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                with client.stream("GET", current_url, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if not location:
                            raise _FetchFailure(
                                f"web_fetch failed: {current_url} redirected without a Location header. Request the destination URL directly."
                            )
                        current_url = urllib.parse.urljoin(current_url, location)
                        continue

                    if response.status_code >= 400:
                        reason = _safe_reason(response.reason_phrase)
                        if response.status_code == 404:
                            guidance = "The page does not exist at this URL; verify it or use web_search to find the current one."
                        elif response.status_code in {401, 403}:
                            guidance = "The server refused anonymous access; try a public URL or another source."
                        elif response.status_code == 429:
                            guidance = "The server is rate limiting; wait and retry, or fetch another URL."
                        elif response.status_code >= 500:
                            guidance = "The server failed to answer; retry later or use another source."
                        else:
                            guidance = "The URL was rejected; verify it or use another source."
                        raise _FetchFailure(f"web_fetch failed: HTTP {response.status_code} {reason} for {str(response.url)}. {guidance}")

                    final_url = str(response.url)
                    content_type = response.headers.get("Content-Type", "")
                    mime = content_type.split(";", 1)[0].strip().lower() if content_type else ""
                    is_attachment = format_value == "markdown" and mime in RASTER_IMAGE_MIMES
                    textual = _is_textual(mime)
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            parsed_length = int(content_length)
                        except ValueError:
                            parsed_length = None
                        if parsed_length is not None and parsed_length > MAX_RESPONSE_SIZE:
                            raise _FetchFailure(_too_large_message(final_url))

                    total = 0
                    chunks: list[bytes] = []
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_SIZE:
                            raise _FetchFailure(_too_large_message(final_url))
                        chunks.append(chunk)
                    data = b"".join(chunks)

                    used_encoding: str | None = None
                    content = ""
                    if textual:
                        used_encoding = response.encoding or "utf-8"
                        try:
                            content = data.decode(used_encoding)
                        except LookupError, UnicodeDecodeError:
                            raise _FetchFailure(
                                f"web_fetch failed: {final_url} declared charset {used_encoding!r} but its bytes "
                                "do not decode with it. Retry, or fetch a different representation of the page."
                            ) from None
                    return _Fetched(
                        original_url=original_url,
                        final_url=final_url,
                        content_type=content_type,
                        mime=mime,
                        data=data,
                        content=content,
                        used_encoding=used_encoding,
                        is_textual=textual,
                        is_attachment=is_attachment,
                    )
            raise _FetchFailure(
                f"web_fetch failed: more than {max_redirects} redirects while fetching {original_url}. Resolve the final URL and fetch it directly."
            )
    except httpx.TimeoutException:
        raise _FetchFailure(
            f"web_fetch failed: the request to {original_url} timed out after {timeout}s. Retry with a larger timeout or a different URL."
        ) from None
    except httpx.ConnectError:
        raise _FetchFailure(f"web_fetch failed: could not connect to {original_url}. Check the host and network access, then retry.") from None
    except httpx.UnsupportedProtocol:
        raise _FetchFailure(
            f"web_fetch failed: {original_url!r} uses a scheme web_fetch cannot fetch. Provide an absolute http:// or https:// URL."
        ) from None
    except httpx.InvalidURL:
        raise _FetchFailure(f"web_fetch failed: {original_url!r} is not a valid absolute URL. Provide an http:// or https:// URL.") from None
    except httpx.HTTPError as exc:
        raise _FetchFailure(
            f"web_fetch failed: {original_url} could not be fetched ({type(exc).__name__}). "
            "Retry once; if it persists, try another URL or report the transport error."
        ) from None


def _continuation_manifest(
    *,
    format_value: str,
    original_url: str,
    final_url: str,
    raw_sha256: str,
    rendered_sha256: str,
    rendered_bytes: int,
) -> bytes:
    return json.dumps(
        {
            "kind": CONTINUATION_KIND,
            "version": CONTINUATION_VERSION,
            "format": format_value,
            "original_url": original_url,
            "final_url": final_url,
            "raw_sha256": raw_sha256,
            "rendered_sha256": rendered_sha256,
            "rendered_bytes": rendered_bytes,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _invalid_ref() -> _FetchFailure:
    return _FetchFailure(
        "web_fetch continuation failed: content_ref must be the voidcode://artifact/<sha256> value "
        "returned by a previous web_fetch result; pass it verbatim."
    )


def _load_manifest(services: PluginServices, content_ref: str) -> dict[str, object]:
    """Read and validate one bounded continuation manifest; never parses a page envelope."""
    if not content_ref.startswith(CONTENT_REF_PREFIX):
        raise _invalid_ref()
    manifest_id = content_ref[len(CONTENT_REF_PREFIX) :]
    if ARTIFACT_ID_PATTERN.fullmatch(manifest_id) is None:
        raise _invalid_ref()
    page = read_artifact_page(services, manifest_id, byte_limit=MAX_MANIFEST_BYTES)
    if page is None:
        raise _FetchFailure(
            f"web_fetch continuation failed: content_ref {content_ref} is not available in this session's "
            "artifact store. Fetch the URL again to obtain a fresh cursor."
        )
    if not page["eof"]:
        raise _FetchFailure(
            f"web_fetch continuation failed: content_ref {content_ref} is larger than a web_fetch continuation "
            "manifest. Fetch the URL again for a fresh cursor."
        )
    try:
        manifest = json.loads(cast(bytes, page["data"]).decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        manifest = None
    if not isinstance(manifest, dict) or manifest.get("kind") != CONTINUATION_KIND or manifest.get("version") != CONTINUATION_VERSION:
        raise _FetchFailure(
            f"web_fetch continuation failed: content_ref {content_ref} does not name a {CONTINUATION_KIND} "
            f"version {CONTINUATION_VERSION} manifest. Fetch the URL again for a fresh cursor."
        )
    rendered_sha256 = manifest.get("rendered_sha256")
    rendered_bytes = manifest.get("rendered_bytes")
    if (
        not isinstance(rendered_sha256, str)
        or ARTIFACT_ID_PATTERN.fullmatch(rendered_sha256) is None
        or type(rendered_bytes) is not int
        or rendered_bytes < 0
        or manifest.get("format") not in RENDERED_MIME_BY_FORMAT
        or not isinstance(manifest.get("original_url"), str)
        or not isinstance(manifest.get("final_url"), str)
    ):
        raise _FetchFailure(
            f"web_fetch continuation failed: content_ref {content_ref} names a malformed web_fetch manifest. Fetch the URL again for a fresh cursor."
        )
    return manifest


def _decode_page(data: bytes) -> tuple[str, int]:
    """Decode one page and drop a trailing partial UTF-8 sequence (≤3 bytes).

    Rendered bytes are valid UTF-8, so the only possible split is at the page end; backing
    off keeps the reported cursor on a character boundary and drops nothing.
    """
    for cut in range(len(data), max(len(data) - 4, -1), -1):
        try:
            return data[:cut].decode("utf-8"), cut
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), len(data)


type _PageReader = Callable[[int, int], tuple[bytes, int]]


def _cursor_head(*, prefix: str, content_ref: str | None, offset: int, end: int, total: int, eof: bool) -> str:
    if eof:
        return f"{prefix} | bytes {offset}-{end} of {total} | complete (eof); no continuation needed"
    if content_ref is None:
        return f"{prefix} | bytes {offset}-{end} of {total} | truncated; continuation unavailable (artifact store absent)"
    return f'{prefix} | bytes {offset}-{end} of {total} | truncated; next call: web_fetch {{"content_ref": "{content_ref}", "byte_offset": {end}}}'


def _render_page(
    *,
    reader: _PageReader,
    offset: int,
    body_for: Callable[[int, bool, int], WebFetchResultBody | WebFetchContinuationBody],
    head_for: Callable[[int, bool, int], str],
) -> tuple[WebFetchResultBody | WebFetchContinuationBody, str, int, bool, int]:
    """Read one page and keep ``body JSON + guidance + text`` inside the model projection."""
    limit = MAX_ARTIFACT_PAGE_BYTES
    while True:
        data, total = reader(offset, limit)
        if data and data[0] & 0xC0 == 0x80:
            raise _FetchFailure(
                "web_fetch continuation failed: byte_offset is not on a UTF-8 character boundary. "
                "Pass the previous result's next_byte_offset verbatim."
            )
        page_text, consumed = _decode_page(data)
        end = offset + consumed
        eof = end >= total
        body = body_for(end, eof, total)
        output = f"{head_for(end, eof, total)}\n\n{page_text}"
        projected = len(json.dumps(body.as_payload(), ensure_ascii=False, allow_nan=False).encode("utf-8")) + 1 + len(output.encode("utf-8"))
        excess = projected - TOOL_OUTPUT_BYTES
        if excess <= 0 or limit <= MIN_PAGE_BYTES:
            return body, output, end, eof, total
        limit = max(MIN_PAGE_BYTES, limit - excess)


def _initial_result(
    definition: ToolDefinition,
    args: WebFetchArgs,
    *,
    services: PluginServices,
) -> ToolResult:
    timeout = max(min(args.timeout, 120), 1)

    fetched = _perform_fetch(original_url=args.url, format_value=args.format, timeout=timeout)
    source_artifact = create_artifact(
        services,
        kind="web_fetch_response",
        content=fetched.data,
        mime=fetched.mime or "application/octet-stream",
        source=fetched.final_url,
    )
    raw_uri = artifact_uri(source_artifact)
    raw_reference = raw_uri or "unavailable"
    provenance = (
        f"Source URL: {fetched.original_url} | Final URL: {fetched.final_url} | "
        f"MIME: {fetched.mime or 'application/octet-stream'} | charset: {fetched.used_encoding or 'unspecified'} | raw: {raw_reference}"
    )
    raw_bounds = (
        OutputBounds(
            reference=OutputReference(uri=raw_uri, artifact=source_artifact),
            source="web_fetch",
        )
        if raw_uri is not None and source_artifact is not None
        else OutputBounds(source="web_fetch")
    )

    if fetched.is_attachment:
        fits_inline = len(fetched.data) <= MAX_INLINE_ATTACHMENT_BYTES
        data_uri = f"data:{fetched.mime};base64,{base64.b64encode(fetched.data).decode('ascii')}" if fits_inline else None
        presentation = f"{provenance}\nFetched {fetched.mime} attachment."
        if data_uri is None:
            presentation += f" Inline bytes omitted above {MAX_INLINE_ATTACHMENT_BYTES} bytes; raw: {raw_reference}"
        body = WebFetchResultBody(
            original_url=fetched.original_url,
            final_url=fetched.final_url,
            content_type=fetched.content_type,
            mime=fetched.mime,
            format=args.format,
            byte_count=len(fetched.data),
            timeout_seconds=timeout,
            used_encoding=fetched.used_encoding,
            provenance="web_fetch",
            source_artifact={} if source_artifact is None else dict(source_artifact),
            content_artifact={},
            content_ref=None,
            byte_offset=0,
            next_byte_offset=None,
            total_bytes=0,
            eof=True,
            truncated=False,
        )
        return ToolSuccess(
            tool_name=definition.name,
            output=AttachmentOutput(
                mime=fetched.mime,
                data_uri=data_uri,
                presentation=presentation,
                bounds=raw_bounds,
            ),
            body=body,
        )

    if not fetched.is_textual:
        body = WebFetchResultBody(
            original_url=fetched.original_url,
            final_url=fetched.final_url,
            content_type=fetched.content_type,
            mime=fetched.mime,
            format=args.format,
            byte_count=len(fetched.data),
            timeout_seconds=timeout,
            used_encoding=fetched.used_encoding,
            provenance="web_fetch",
            source_artifact={} if source_artifact is None else dict(source_artifact),
            content_artifact={},
            content_ref=None,
            byte_offset=0,
            next_byte_offset=None,
            total_bytes=0,
            eof=True,
            truncated=False,
        )
        return ToolSuccess(
            tool_name=definition.name,
            output=TextOutput(
                f"{provenance}\n\nOpaque response bytes were preserved without text decoding.",
                presentation=f"Fetched {len(fetched.data)} bytes from {fetched.final_url}.",
                bounds=raw_bounds,
            ),
            body=body,
        )

    if args.format == "html":
        rendered = fetched.content
    elif args.format == "text":
        rendered = _extract_text_from_html(fetched.content)
    elif fetched.mime in {"text/html", "application/xhtml+xml"}:
        rendered = _html_to_markdown_or_text(fetched.content, url=fetched.final_url)
    else:
        rendered = fetched.content
    rendered_bytes = rendered.encode("utf-8")

    rendered_artifact = create_artifact(
        services,
        kind="web_fetch_content",
        content=rendered_bytes,
        mime=RENDERED_MIME_BY_FORMAT[args.format],
    )
    manifest_artifact = None
    if rendered_artifact is not None:
        manifest_data = _continuation_manifest(
            format_value=args.format,
            original_url=fetched.original_url,
            final_url=fetched.final_url,
            raw_sha256="" if source_artifact is None else str(source_artifact.get("sha256", "")),
            rendered_sha256=str(rendered_artifact.get("sha256", "")),
            rendered_bytes=len(rendered_bytes),
        )
        if len(manifest_data) <= MAX_MANIFEST_BYTES:
            manifest_artifact = create_artifact(services, kind=CONTINUATION_KIND, content=manifest_data, mime="application/json")
    content_ref = artifact_uri(manifest_artifact)
    content_uri = artifact_uri(rendered_artifact)
    reference_artifact = rendered_artifact if rendered_artifact is not None else source_artifact
    reference_uri = content_uri or raw_uri

    def read_bytes(offset: int, limit: int) -> tuple[bytes, int]:
        return rendered_bytes[offset : offset + limit], len(rendered_bytes)

    def body_for(end: int, eof: bool, total: int) -> WebFetchResultBody:
        return WebFetchResultBody(
            original_url=fetched.original_url,
            final_url=fetched.final_url,
            content_type=fetched.content_type,
            mime=fetched.mime,
            format=args.format,
            byte_count=len(fetched.data),
            timeout_seconds=timeout,
            used_encoding=fetched.used_encoding,
            provenance="web_fetch",
            source_artifact={} if source_artifact is None else dict(source_artifact),
            content_artifact={} if manifest_artifact is None else dict(manifest_artifact),
            content_ref=content_ref,
            byte_offset=0,
            next_byte_offset=None if eof or content_ref is None else end,
            total_bytes=total,
            eof=eof,
            truncated=not eof,
        )

    def head_for(end: int, eof: bool, total: int) -> str:
        prefix = provenance + (f" | rendered: {content_uri}" if content_uri is not None else "")
        return _cursor_head(prefix=prefix, content_ref=content_ref, offset=0, end=end, total=total, eof=eof)

    body, output, _end, eof, _total = _render_page(reader=read_bytes, offset=0, body_for=body_for, head_for=head_for)
    bounds = (
        OutputBounds(
            reference=OutputReference(uri=reference_uri, artifact=reference_artifact),
            truncated=not eof,
            source="web_fetch",
        )
        if reference_uri is not None and reference_artifact is not None
        else OutputBounds(truncated=not eof, source="web_fetch")
    )
    return ToolSuccess(
        tool_name=definition.name,
        output=TextOutput(
            output,
            presentation=f"Fetched {len(rendered_bytes)} bytes from {fetched.final_url} as {args.format}.",
            bounds=bounds,
        ),
        body=body,
    )


def _continuation_result(
    definition: ToolDefinition,
    args: WebFetchContinuationArgs,
    *,
    services: PluginServices,
) -> ToolResult:
    manifest = _load_manifest(services, args.content_ref)
    total = cast(int, manifest["rendered_bytes"])
    rendered_sha256 = str(manifest["rendered_sha256"])
    if args.byte_offset > total:
        raise _FetchFailure(
            f"web_fetch continuation failed: byte_offset {args.byte_offset} is past the end of {args.content_ref} "
            f"({total} bytes). Pass a value between 0 and {total}."
        )

    def read_bytes(offset: int, limit: int) -> tuple[bytes, int]:
        page = read_artifact_page(services, rendered_sha256, byte_offset=offset, byte_limit=limit)
        if page is None:
            raise _FetchFailure(
                f"web_fetch continuation failed: rendered bytes for {args.content_ref} are no longer available "
                "in this session's artifact store. Fetch the URL again for a fresh cursor."
            )
        if cast(int, page["total_bytes"]) != total:
            raise _FetchFailure(
                f"web_fetch continuation failed: {args.content_ref} does not match its stored rendered bytes. Fetch the URL again for a fresh cursor."
            )
        return cast(bytes, page["data"]), total

    format_value = str(manifest["format"])
    original_url = str(manifest["original_url"])
    final_url = str(manifest["final_url"])
    raw_sha256 = manifest.get("raw_sha256")
    raw_reference = "unavailable"
    if isinstance(raw_sha256, str) and ARTIFACT_ID_PATTERN.fullmatch(raw_sha256):
        raw_reference = f"{CONTENT_REF_PREFIX}{raw_sha256}"
    prefix = f"Source URL: {original_url} | Final URL: {final_url} | format: {format_value} | raw: {raw_reference}"

    def body_for(end: int, eof: bool, page_total: int) -> WebFetchContinuationBody:
        return WebFetchContinuationBody(
            content_ref=args.content_ref,
            format=format_value,
            original_url=original_url,
            final_url=final_url,
            byte_offset=args.byte_offset,
            next_byte_offset=None if eof else end,
            total_bytes=page_total,
            eof=eof,
            truncated=not eof,
            provenance="web_fetch continuation",
        )

    def head_for(end: int, eof: bool, page_total: int) -> str:
        return _cursor_head(prefix=prefix, content_ref=args.content_ref, offset=args.byte_offset, end=end, total=page_total, eof=eof)

    body, output, _end, eof, _total = _render_page(reader=read_bytes, offset=args.byte_offset, body_for=body_for, head_for=head_for)
    return ToolSuccess(
        tool_name=definition.name,
        output=TextOutput(
            output,
            presentation=f"Fetched continuation page of {args.content_ref}.",
            bounds=OutputBounds(truncated=not eof, source="web_fetch"),
        ),
        body=body,
    )
