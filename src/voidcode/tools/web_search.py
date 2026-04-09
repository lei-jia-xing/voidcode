from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import ClassVar

from .contracts import ToolCall, ToolDefinition, ToolResult

DEFAULT_NUM_RESULTS = 8
DEFAULT_TIMEOUT = 30


def _search_exa(query: str, num_results: int = DEFAULT_NUM_RESULTS) -> str | None:
    try:
        import os

        api_key = os.environ.get("EXA_API_KEY")
    except Exception:
        api_key = None

    if not api_key:
        return None

    try:
        request_data: dict[str, object] = {
            "q": query,
            "numResults": num_results,
            "type": "neural",
        }

        req = urllib.request.Request(
            "https://api.exa.ai/search",
            data=json.dumps(request_data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

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


def _search_fallback(query: str, num_results: int = DEFAULT_NUM_RESULTS) -> str:
    encoded_query = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}&kl=wt-wt"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; VoidCode/1.0)",
            },
        )

        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as response:
            html = response.read().decode("utf-8", errors="replace")

            results: list[tuple[str, str, str]] = []
            pattern = r'<a class="result__a" href="([^"]+)"[^>]*>([^<]+)</a>'
            snippet_pattern = r'<a class="result__snippet"[^>]*>([^<]+)</a>'

            for match in re.finditer(pattern, html):
                url_match = match.group(1)
                title = match.group(2).strip()
                snippet_match = re.search(
                    snippet_pattern, html[match.start() : match.start() + 500]
                )
                snippet = snippet_match.group(1).strip() if snippet_match else ""

                results.append((title, url_match, snippet))

                if len(results) >= num_results:
                    break

            if results:
                output_lines: list[str] = []
                for i, (title, url, snippet) in enumerate(results, 1):
                    output_lines.append(f"{i}. {title}")
                    output_lines.append(f"   {url}")
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
        query_value = call.arguments.get("query")
        if not isinstance(query_value, str):
            raise ValueError("web_search requires a string query argument")

        if not query_value.strip():
            raise ValueError("web_search query must not be empty")

        num_results = call.arguments.get("numResults", DEFAULT_NUM_RESULTS)
        if isinstance(num_results, (int, float)) and num_results > 0:
            num_results = min(int(num_results), 20)
        else:
            num_results = DEFAULT_NUM_RESULTS

        exa_results = _search_exa(query_value, num_results)

        if exa_results:
            output = exa_results
            source = "exa"
        else:
            output = _search_fallback(query_value, num_results)
        source = "duckduckgo"

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=output,
            data={
                "query": query_value,
                "num_results": num_results,
                "source": source,
            },
        )
