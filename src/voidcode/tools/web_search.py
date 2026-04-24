from __future__ import annotations

import os
import re
from pathlib import Path
from typing import ClassVar

import httpx
from pydantic import ValidationError

from ._pydantic_args import WebSearchArgs
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
            html = response.text

        results: list[tuple[str, str, str]] = []
        pattern = r'<a class="result__a" href="([^"]+)"[^>]*>([^<]+)</a>'
        snippet_pattern = r'<a class="result__snippet"[^>]*>([^<]+)</a>'

        for match in re.finditer(pattern, html):
            url_match = match.group(1)
            title = match.group(2).strip()
            snippet_match = re.search(snippet_pattern, html[match.start() : match.start() + 500])
            snippet = snippet_match.group(1).strip() if snippet_match else ""

            results.append((title, url_match, snippet))

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
        try:
            args = WebSearchArgs.model_validate({"query": call.arguments.get("query")})
        except ValidationError as exc:
            first_error = exc.errors()[0]
            if first_error.get("type") == "value_error":
                raise ValueError("web_search query must not be empty") from exc
            raise ValueError("web_search requires a string query argument") from exc

        num_results = call.arguments.get("numResults", DEFAULT_NUM_RESULTS)
        if isinstance(num_results, (int, float)) and num_results > 0:
            num_results = min(int(num_results), 20)
        else:
            num_results = DEFAULT_NUM_RESULTS

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
