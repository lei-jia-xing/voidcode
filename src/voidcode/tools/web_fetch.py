from __future__ import annotations

import base64
import ipaddress
import re
import socket
import urllib.parse
from pathlib import Path
from typing import ClassVar

import httpx
from bs4 import BeautifulSoup

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


def _extract_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in ("script", "style", "noscript", "iframe", "object", "embed"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for tag in soup.find_all(["br", "li", "p", "div", "section", "article", "tr"]):
        tag.append("\n")

    result = soup.get_text(separator=" ")
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r" *\n *", "\n", result)
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
        return self._invoke(call, workspace=workspace, runtime_timeout_seconds=None)

    def invoke_with_runtime_timeout(
        self, call: ToolCall, *, workspace: Path, timeout_seconds: int
    ) -> ToolResult:
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

        _validate_fetch_url(url_value)

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
                            raise ValueError(
                                "Failed to fetch URL: redirect response missing location"
                            )
                        redirected_url = urllib.parse.urljoin(current_url, location)
                        _validate_fetch_url(redirected_url)
                        current_url = redirected_url
                        continue

                    if response.status_code >= 400:
                        raise ValueError(
                            f"HTTP error {response.status_code}: {response.reason_phrase}"
                        )

                    final_url = str(response.url)
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
                    attachment={"mime": mime, "data_uri": data_uri},
                    timeout_seconds=timeout,
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
                        "timeout_seconds": timeout,
                        "attachment": {"mime": mime, "data_uri": data_uri},
                    },
                    truncated=False,
                    partial=False,
                    attachment={"mime": mime, "data_uri": data_uri},
                    timeout_seconds=timeout,
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
                "timeout_seconds": timeout,
            },
            truncated=False,
            partial=False,
            timeout_seconds=timeout,
        )
