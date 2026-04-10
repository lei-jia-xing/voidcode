from __future__ import annotations

import base64
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, ClassVar, cast

from .contracts import ToolCall, ToolDefinition, ToolResult

MAX_RESPONSE_SIZE = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30


def _is_private_or_loopback_host(hostname: str) -> bool:
    blocked_hostnames = {
        "localhost",
        "metadata.google.internal",
        "metadata",
    }
    lower = hostname.lower().strip(".")
    if lower in blocked_hostnames:
        return True

    try:
        ip = ipaddress.ip_address(lower)
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def _validate_fetch_url(url_value: str) -> None:
    parsed = urllib.parse.urlparse(url_value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("web_fetch url must start with http:// or https://")

    if not parsed.hostname:
        raise ValueError("web_fetch url must include a hostname")

    if _is_private_or_loopback_host(parsed.hostname):
        raise ValueError("web_fetch target host is blocked for security reasons")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        _validate_fetch_url(newurl)
        return super().redirect_request(
            req,
            cast(IO[bytes], fp),
            code,
            msg,
            cast(HTTPMessage, headers),
            newurl,
        )


def _extract_text_from_html(html: str) -> str:
    text: list[str] = []
    skip_content = False

    script_style_tags = {"script", "style", "noscript", "iframe", "object", "embed"}
    tag_stack: list[str] = []

    i = 0
    while i < len(html):
        if html[i] == "<":
            if html.startswith("<!--", i):
                end_comment = html.find("-->", i + 4)
                if end_comment == -1:
                    break
                i = end_comment + 3
                continue

            j = html.find(">", i)
            if j == -1:
                text.append(html[i:])
                break

            tag_raw = html[i + 1 : j].strip()
            if not tag_raw:
                i = j + 1
                continue

            parts = tag_raw.split()
            if not parts:
                i = j + 1
                continue
            tag = parts[0].lower().rstrip("/")

            if tag.startswith("!"):
                i = j + 1
                continue

            is_closing = tag.startswith("/")
            clean_tag = tag.lstrip("/")

            if is_closing and tag_stack and tag_stack[-1] == clean_tag:
                tag_stack.pop()
                if clean_tag in script_style_tags:
                    skip_content = False
            elif not is_closing:
                tag_stack.append(clean_tag)
                if clean_tag in script_style_tags:
                    skip_content = True

            if not skip_content:
                if (
                    html[i : i + 7] == "<br"
                    or html[i : i + 9] == "<br/>"
                    or html[i : i + 10] == "<br />"
                ):
                    text.append("\n")

            i = j + 1
            continue

        if not skip_content and i < len(html):
            text.append(html[i])

        i += 1

    result = "".join(text)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"[ \t]+", " ", result)
    return result.strip()


def _convert_html_to_markdown(html: str) -> str:
    # Improve HTML->Markdown conversion by applying simple heuristics on extracted text
    lines = _extract_text_from_html(html).split("\n")
    markdown_lines: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            markdown_lines.append("")
            continue

        # Normalize emphasis markers already present
        line = re.sub(r"\*\*(.+?)\*\*", r"**\1**", line)
        line = re.sub(r"\*(.+?)\*", r"*\1*", line)
        line = re.sub(r"__(.+?)__", r"_\1_", line)
        line = re.sub(r"_(.+?)_", r"_\1_", line)

        # Heuristic: treat all-uppercase lines as headings
        if line.isupper() and len(line) > 3 and not any(ch.isdigit() for ch in line):
            markdown_lines.append("# " + line.title())
            continue

        markdown_lines.append(line)

    return "\n".join(markdown_lines)


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
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        url_value = call.arguments.get("url")
        if not isinstance(url_value, str):
            raise ValueError("web_fetch requires a string url argument")

        _validate_fetch_url(url_value)

        format_value = call.arguments.get("format", "markdown")
        if not isinstance(format_value, str) or format_value not in ("text", "markdown", "html"):
            raise ValueError("web_fetch format must be 'text', 'markdown', or 'html'")

        timeout_value = call.arguments.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout_value, (int, float)) and timeout_value > 0:
            timeout = min(int(timeout_value), 120)
        else:
            timeout = DEFAULT_TIMEOUT

        content: str = ""
        data: bytes = b""
        mime: str = ""
        opener = urllib.request.build_opener(_SafeRedirectHandler())

        try:
            # Build Accept header according to requested format to be friendlier for servers
            accept_by_format = {
                "markdown": (
                    "text/markdown;q=1.0, text/x-markdown;q=0.9, "
                    "text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"
                ),
                "text": ("text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1"),
                "html": (
                    "text/html;q=1.0, application/xhtml+xml;q=0.9, "
                    "text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1"
                ),
            }

            accept_header = accept_by_format.get(format_value, "*/*")

            # Use a more realistic User-Agent to avoid bot detection on some servers
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "VoidCode/1.0 Chrome/110.0.5481.100 Safari/537.36"
            )

            req = urllib.request.Request(
                url_value,
                headers={
                    "User-Agent": ua,
                    "Accept": accept_header,
                },
            )

            with opener.open(req, timeout=timeout) as response:
                final_url = response.geturl()
                _validate_fetch_url(final_url)
                content_type = response.headers.get("Content-Type", "")
                content_length = response.headers.get("Content-Length")

                if content_length:
                    try:
                        parsed_length = int(content_length)
                    except (TypeError, ValueError):
                        parsed_length = None
                    if parsed_length is not None and parsed_length > MAX_RESPONSE_SIZE:
                        limit_mb = MAX_RESPONSE_SIZE // 1024 // 1024
                        raise ValueError(f"Response too large (exceeds {limit_mb}MB limit)")

                chunks: list[bytes] = []
                total = 0
                chunk_size = 64 * 1024
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RESPONSE_SIZE:
                        limit_mb = MAX_RESPONSE_SIZE // 1024 // 1024
                        raise ValueError(f"Response too large (exceeds {limit_mb}MB limit)")
                    chunks.append(chunk)

                data = b"".join(chunks)

                content = data.decode("utf-8", errors="replace")
                mime = content_type.split(";")[0].strip().lower() if content_type else ""

        except urllib.error.HTTPError as exc:
            raise ValueError(f"HTTP error {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"Failed to fetch URL: {exc.reason}") from exc

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
                        "attachment": {"mime": mime, "data_uri": data_uri},
                    },
                )
            if "text/html" in mime:
                output = _convert_html_to_markdown(content)
            else:
                output = content
        else:
            if mime and mime.startswith("image/"):
                # Image handling: return as base64 attachment instead of text output
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
                        "attachment": {"mime": mime, "data_uri": data_uri},
                    },
                )
            if "text/html" in mime:
                output = _convert_html_to_markdown(content)
            else:
                output = content

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "url": url_value,
                "content_type": mime,
                "format": format_value,
                "byte_count": len(data),
            },
        )
