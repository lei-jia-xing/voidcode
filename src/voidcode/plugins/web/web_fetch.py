from __future__ import annotations

import codecs
import logging
import time
from dataclasses import dataclass
from typing import ClassVar, final
from urllib.parse import urljoin, urlsplit

import httpx
from markdownify import MarkdownConverter
from pydantic import BaseModel, ConfigDict, field_validator

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    EmptyOutput,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

logger = logging.getLogger(__name__)

#: Fallback httpx per-operation timeout (connect/read/write/pool idle). Each request is
#: additionally bounded by the remaining share of TOTAL_FETCH_SECONDS; the monotonic
#: deadline is advisory (checked between chunks/redirects), not a preemptive kill of a
#: single blocked httpx read.
TIMEOUT_SECONDS = 30
#: Advisory wall-clock budget for the whole fetch, enforced via monotonic deadline checks.
TOTAL_FETCH_SECONDS = 30
MAX_REDIRECTS = 5
#: Decompressed response body cap; Content-Length alone is never trusted.
FETCH_MAX_BYTES = 5 * 1024 * 1024
WEB_URI_PREFIX = "web://"
_MAX_URL_CHARS = 8192
_MAX_LOCATION_CHARS = 2048
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_FETCH_HEADERS = {
    "User-Agent": "voidcode-web-fetch",
    "Accept": "text/html, text/*;q=0.9, application/json;q=0.8, */*;q=0.1",
}


def _validated_url(value: str) -> str:
    """Cleaned URL or a safe ValueError that never echoes credentials."""
    cleaned = value.strip()
    if len(cleaned) > _MAX_URL_CHARS:
        raise ValueError("url is too long")
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        raise ValueError("url must be a valid http(s) URL") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("url must be an http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url must not include credentials")
    if not parts.hostname:
        raise ValueError("url must include a host")
    return cleaned


class _AbsoluteLinkConverter(MarkdownConverter):
    """Markdownify converter that resolves href/src against the final fetch URL."""

    base_url: str

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url

    @staticmethod
    def _absolute(base_url: str, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or cleaned.startswith(("#", "data:", "mailto:", "javascript:")):
            return value
        return urljoin(base_url, cleaned)

    def convert_a(self, el, text, parent_tags):  # type: ignore[no-untyped-def]
        href = el.get("href")
        if isinstance(href, str):
            el["href"] = self._absolute(self.base_url, href)
        return super().convert_a(el, text, parent_tags)  # ty: ignore[missing-attribute]

    def convert_img(self, el, text, parent_tags):  # type: ignore[no-untyped-def]
        src = el.get("src")
        if isinstance(src, str):
            el["src"] = self._absolute(self.base_url, src)
        return super().convert_img(el, text, parent_tags)  # ty: ignore[missing-attribute]


def _render_html(html: str, base_url: str) -> str:
    """Convert HTML with relative href/src resolved against the final URL, in one parse."""
    return _AbsoluteLinkConverter(base_url).convert(html)


class WebFetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    url: str

    @field_validator("url", mode="after")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url must not be empty")
        return _validated_url(value)


@dataclass(frozen=True, slots=True)
class WebFetchResultBody:
    url: str
    final_url: str
    content_type: str
    uri: str
    artifact: ArtifactReference
    truncated: bool
    bytes: int

    def as_payload(self) -> dict[str, object]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "uri": self.uri,
            "artifact": self.artifact.as_payload(),
            "truncated": self.truncated,
            "bytes": self.bytes,
        }


def _supported_text_mime(mime: str) -> bool:
    if mime.startswith("text/") or mime in _HTML_MIMES:
        return True
    if mime in {"application/json", "application/xml", "application/javascript", "application/x-javascript"}:
        return True
    return mime.endswith("+xml") or mime.endswith("+json")


def _decode_body(raw: bytes, charset: str | None) -> str:
    """Decode with the declared charset; unknown names and undecodable bytes fail explicitly."""
    name = (charset or "utf-8").strip() or "utf-8"
    try:
        info = codecs.lookup(name)
    except LookupError:
        raise ValueError(f"web_fetch response declares unsupported charset {name[:64]}.") from None
    if info.name in {"base64", "hex", "rot-13", "rot13", "quopri", "uu", "zlib", "bz2", "base64-codec", "hex-codec"}:
        raise ValueError(f"web_fetch response declares non-text charset {name[:64]}.")
    try:
        return raw.decode(info.name, errors="strict")
    except (
        UnicodeError,
        LookupError,
        ValueError,
    ):
        raise ValueError(f"web_fetch response body is not valid {info.name}.") from None


def _is_cancelled(context: ToolContext) -> bool:
    return context.abort_signal is not None and context.abort_signal.cancelled


def _fail(tool_name: str, message: str, *, source: str | None = None, timeout: int | None = None) -> ToolFailure:
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(message, bounds=OutputBounds(source=source)),
        timeout_seconds=timeout,
    )


def _cancelled(tool_name: str, source: str) -> ToolFailure:
    message = "web_fetch cancelled"
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(message, bounds=OutputBounds(partial=True, source=source)),
        execution=ConfirmedStop(cancellation_signalled=True),
    )


@final
class WebFetchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="web_fetch",
        icon=Icon(glyph="⌕"),
        description="Fetch a web page over HTTP(S). Returns the page text as Markdown plus a full-content artifact for continuation.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http(s) URL to fetch"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.NETWORK}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        args = WebFetchArgs.model_validate(dict(call.arguments))
        requested_url = args.url
        tool_name = self.definition.name
        if _is_cancelled(context):
            return _cancelled(tool_name, requested_url)
        deadline = time.monotonic() + TOTAL_FETCH_SECONDS
        current_url = requested_url
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                if _is_cancelled(context):
                    return _cancelled(tool_name, current_url)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    message = f"web_fetch exceeded {TOTAL_FETCH_SECONDS}s total fetch time."
                    return _fail(tool_name, message, source=current_url, timeout=TOTAL_FETCH_SECONDS)
                try:
                    # Bound this request by the remaining advisory budget, not the full idle timeout.
                    with client.stream("GET", current_url, headers=_FETCH_HEADERS, timeout=min(TIMEOUT_SECONDS, remaining)) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location", "").strip()
                            if not location or len(location) > _MAX_LOCATION_CHARS:
                                message = f"web_fetch failed with HTTP {response.status_code}."
                                return _fail(tool_name, message, source=current_url)
                            try:
                                current_url = _validated_url(urljoin(current_url, location))
                            except ValueError:
                                return _fail(tool_name, "web_fetch redirect target must be an http(s) URL without credentials.", source=current_url)
                            continue
                        if response.status_code < 200 or response.status_code >= 300:
                            message = f"web_fetch failed with HTTP {response.status_code}."
                            return _fail(tool_name, message, source=current_url)
                        content_type = response.headers.get("content-type", "")
                        mime = content_type.split(";", 1)[0].strip().lower()[:128]
                        if not _supported_text_mime(mime):
                            message = f"web_fetch does not support Content-Type {mime or 'unknown'}."
                            return _fail(tool_name, message, source=current_url)
                        raw_chunks: list[bytes] = []
                        raw_size = 0
                        # Unbuffered decoded chunks: every network yield hits the deadline/cancel checks.
                        for chunk in response.iter_bytes():
                            if _is_cancelled(context):
                                return _cancelled(tool_name, current_url)
                            if time.monotonic() > deadline:
                                message = f"web_fetch exceeded {TOTAL_FETCH_SECONDS}s total fetch time."
                                return _fail(tool_name, message, source=current_url, timeout=TOTAL_FETCH_SECONDS)
                            raw_size += len(chunk)
                            if raw_size > FETCH_MAX_BYTES:
                                message = f"web_fetch response exceeds {FETCH_MAX_BYTES}-byte limit."
                                return _fail(tool_name, message, source=current_url)
                            raw_chunks.append(chunk)
                        if _is_cancelled(context):
                            return _cancelled(tool_name, current_url)
                        raw = b"".join(raw_chunks)
                        try:
                            text = _decode_body(raw, response.charset_encoding)
                        except ValueError as exc:
                            return _fail(tool_name, str(exc), source=current_url)
                        if mime in _HTML_MIMES:
                            try:
                                rendered_text = _render_html(text, current_url)
                            except Exception:
                                logger.warning("web_fetch HTML conversion failed for %s", current_url, exc_info=True)
                                message = "web_fetch could not convert HTML response."
                                return _fail(tool_name, message, source=current_url)
                            rendered_mime = "text/markdown; charset=utf-8"
                        else:
                            rendered_text = text
                            rendered_mime = f"{mime}; charset=utf-8"
                        rendered_bytes = rendered_text.encode("utf-8")
                        artifact = services.spill_artifact(rendered_bytes, media_type=rendered_mime)
                        web_uri = f"{WEB_URI_PREFIX}{artifact.sha256}"
                        return ToolSuccess(
                            tool_name=tool_name,
                            output=EmptyOutput(
                                presentation=f"Fetched {current_url}.",
                                bounds=OutputBounds(reference=artifact, source=current_url),
                            ),
                            body=WebFetchResultBody(
                                url=requested_url,
                                final_url=current_url,
                                content_type=mime,
                                uri=web_uri,
                                artifact=artifact,
                                truncated=False,
                                bytes=len(rendered_bytes),
                            ),
                        )
                except httpx.HTTPError as exc:
                    if isinstance(exc, httpx.TimeoutException):
                        logger.warning("web_fetch timed out for %s", current_url, exc_info=True)
                        return _fail(tool_name, "web_fetch timed out.", source=current_url, timeout=TIMEOUT_SECONDS)
                    if isinstance(exc, httpx.ConnectError):
                        logger.warning("web_fetch connection failed for %s", current_url, exc_info=True)
                        return _fail(tool_name, "web_fetch could not reach the server.", source=current_url)
                    logger.warning("web_fetch request failed for %s", current_url, exc_info=True)
                    return _fail(tool_name, "web_fetch request failed.", source=current_url)
        return _fail(tool_name, f"web_fetch exceeded {MAX_REDIRECTS} redirects.", source=current_url)
