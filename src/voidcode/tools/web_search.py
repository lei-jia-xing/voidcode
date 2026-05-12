from __future__ import annotations

import os
import re
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError

from ._pydantic_args import WebSearchArgs, format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult

DEFAULT_NUM_RESULTS = 8
DEFAULT_TIMEOUT = 30


def _search_exa(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> str | None:
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return None

    try:
        request_data: dict[str, object] = {
            "q": query,
            "numResults": num_results,
            "type": "neural",
        }

        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.exa.ai/search",
                json=request_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            response.raise_for_status()
            data = response.json()

        if "results" in data:
            results = data["results"]
            output_lines: list[str] = []

            for i, result in enumerate(results[:num_results], 1):
                title = result.get("title", "Untitled")
                url = result.get("url", "")
                snippet = result.get("snippet", "")

                output_lines.append(f"{i}. {title}")
                output_lines.append(f"   {url}")
                if snippet:
                    output_lines.append(f"   {snippet[:200]}...")
                output_lines.append("")

            return "\n".join(output_lines)

    except Exception:
        pass

    return None


def _search_fallback(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    url = "https://html.duckduckgo.com/html/"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                url,
                params={"q": query, "kl": "wt-wt"},
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; VoidCode/1.0)",
                },
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

        results: list[tuple[str, str, str]] = []
        anchors = soup.select("a.result__a")
        if not anchors:
            anchors = soup.select("article a[href]")

        for anchor in anchors:
            title = anchor.get_text(" ", strip=True)
            raw_href = anchor.get("href")
            href = raw_href if isinstance(raw_href, str) else ""
            if not title or not href:
                continue

            result_url = _resolve_duckduckgo_result_url(href)
            if not result_url:
                continue

            container = anchor.find_parent("div", class_="result") or anchor.find_parent("article")
            snippet = ""
            if container is not None:
                snippet_node = container.select_one(".result__snippet")
                if snippet_node is None:
                    snippet_node = container.find(
                        ["a", "div", "span"],
                        class_=re.compile("snippet"),
                    )
                if snippet_node is not None:
                    snippet = snippet_node.get_text(" ", strip=True)

            results.append((title, result_url, snippet))

            if len(results) >= num_results:
                break

        if results:
            output_lines: list[str] = []
            for i, (title, result_url, snippet) in enumerate(results, 1):
                output_lines.append(f"{i}. {title}")
                output_lines.append(f"   {result_url}")
                if snippet:
                    clean_snippet = re.sub(r"<[^>]+>", "", snippet)
                    output_lines.append(f"   {clean_snippet[:200]}...")
                output_lines.append("")
            return "\n".join(output_lines)

    except Exception:
        pass

    return "No search results found. Please try a different query."


def _resolve_duckduckgo_result_url(raw_url: str) -> str | None:
    if raw_url.startswith("//"):
        return _resolve_duckduckgo_result_url(f"https:{raw_url}")
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        parsed = urlparse(raw_url)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            uddg = parse_qs(parsed.query).get("uddg")
            if uddg:
                return unquote(uddg[0])
        return raw_url
    if raw_url.startswith("/l/"):
        parsed = urlparse(raw_url)
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return None


class WebSearchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="web_search",
        description=(
            "Search the web for information. Returns search results "
            "with titles, URLs, and snippets."
        ),
        input_schema={
            "query": {"type": "string", "description": "The search query"},
            "numResults": {
                "type": "integer",
                "description": "Number of results to return (default: 8)",
            },
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
        try:
            args = WebSearchArgs.model_validate(
                {
                    "query": call.arguments.get("query"),
                    "numResults": call.arguments.get("numResults", DEFAULT_NUM_RESULTS),
                }
            )
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        num_results = min(args.numResults, 20)

        timeout = (
            DEFAULT_TIMEOUT
            if runtime_timeout_seconds is None
            else min(
                DEFAULT_TIMEOUT,
                runtime_timeout_seconds,
            )
        )

        exa_results = _search_exa(args.query, num_results, timeout)

        if exa_results:
            output = exa_results
            source = "exa"
        else:
            output = _search_fallback(args.query, num_results, timeout)
            source = "duckduckgo"

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "query": args.query,
                "num_results": num_results,
                "source": source,
                "timeout_seconds": timeout,
            },
            timeout_seconds=timeout,
            source=source,
            fallback_reason=None if source == "exa" else "duckduckgo fallback",
        )
