from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import ClassVar, cast

from .contracts import ToolCall, ToolDefinition, ToolResult

DEFAULT_NUM_RESULTS = 5
DEFAULT_CONTEXT_MAX_CHARACTERS = 10000


def _fallback_search(query: str, num_results: int) -> str:
    encoded_query = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}&kl=wt-wt"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; VoidCode/1.0)",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")

            results: list[tuple[str, str, str]] = []
            pattern = r'<a class="result__a" href="([^"]+)"[^>]*>([^<]+)</a>'
            snippet_pattern = r'<a class="result__snippet"[^>]*>([^<]+)</a>'

            for match in re.finditer(pattern, html):
                url_match = match.group(1)
                title = match.group(2).strip()
                snippet_match = re.search(
                    snippet_pattern,
                    html[match.start() : match.start() + 500],
                )
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


class CodeSearchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="code_search",
        description="Search programming examples via Exa MCP web_search_exa.",
        input_schema={
            "query": {"type": "string", "description": "The search query"},
            "numResults": {
                "type": "integer",
                "description": "Number of results to return (default: 5)",
            },
            "livecrawl": {
                "type": "string",
                "enum": ["fallback", "preferred"],
                "description": "Live crawl strategy",
            },
            "type": {
                "type": "string",
                "enum": ["auto", "fast", "deep"],
                "description": "Search depth mode",
            },
            "contextMaxCharacters": {
                "type": "integer",
                "description": "Maximum context characters for extracted snippets",
            },
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        query = call.arguments.get("query")
        if not isinstance(query, str):
            raise ValueError("code_search requires a string query argument")
        if not query.strip():
            raise ValueError("code_search query must not be empty")

        num_results = call.arguments.get("numResults", DEFAULT_NUM_RESULTS)
        if isinstance(num_results, (int, float)):
            num_results = min(max(int(num_results), 1), 20)
        else:
            num_results = DEFAULT_NUM_RESULTS

        livecrawl = call.arguments.get("livecrawl", "fallback")
        if not isinstance(livecrawl, str) or livecrawl not in {"fallback", "preferred"}:
            livecrawl = "fallback"

        search_type = call.arguments.get("type", "auto")
        if not isinstance(search_type, str) or search_type not in {"auto", "fast", "deep"}:
            search_type = "auto"

        context_max_characters = call.arguments.get(
            "contextMaxCharacters",
            call.arguments.get("tokensNum", DEFAULT_CONTEXT_MAX_CHARACTERS),
        )
        if isinstance(context_max_characters, (int, float)):
            context_max_characters = min(max(int(context_max_characters), 1000), 50000)
        else:
            context_max_characters = DEFAULT_CONTEXT_MAX_CHARACTERS

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": query,
                    "numResults": num_results,
                    "livecrawl": livecrawl,
                    "type": search_type,
                    "contextMaxCharacters": context_max_characters,
                },
            },
        }

        request = urllib.request.Request(
            "https://mcp.exa.ai/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                fallback = _fallback_search(query, num_results)
                return ToolResult(
                    tool_name=self.definition.name,
                    status="ok",
                    content=fallback,
                    data={
                        "query": query,
                        "num_results": num_results,
                        "livecrawl": livecrawl,
                        "type": search_type,
                        "context_max_characters": context_max_characters,
                        "source": "duckduckgo_fallback",
                        "snippet_count": 0,
                        "sources": [],
                        "fallback_reason": f"HTTP 429 from Exa MCP: {exc.reason}",
                    },
                )
            raise ValueError(f"code_search HTTP error {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            fallback = _fallback_search(query, num_results)
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=fallback,
                data={
                    "query": query,
                    "num_results": num_results,
                    "livecrawl": livecrawl,
                    "type": search_type,
                    "context_max_characters": context_max_characters,
                    "source": "duckduckgo_fallback",
                    "snippet_count": 0,
                    "sources": [],
                    "fallback_reason": f"code_search request failed: {exc.reason}",
                },
            )

        snippets: list[str] = []
        sources: list[str] = []

        lines = text.splitlines()
        for line in lines:
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if not raw or raw == "[DONE]":
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if not isinstance(data, dict):
                continue
            data_dict = cast(dict[str, object], data)

            error_obj = data_dict.get("error")
            if isinstance(error_obj, dict):
                error_dict = cast(dict[str, object], error_obj)
                message = error_dict.get("message")
                raise ValueError(
                    str(message)
                    if isinstance(message, str)
                    else f"code_search MCP error: {error_obj}"
                )

            result = data_dict.get("result")
            if not isinstance(result, dict):
                continue
            result_dict = cast(dict[str, object], result)
            content = result_dict.get("content")
            if isinstance(content, list) and content:
                content_items = cast(list[object], content)
                for item_obj in content_items:
                    if not isinstance(item_obj, dict):
                        continue
                    item_dict = cast(dict[str, object], item_obj)
                    text_value = item_dict.get("text")
                    if isinstance(text_value, str) and text_value.strip():
                        snippets.append(text_value.strip())

                    source_url = item_dict.get("url")
                    if isinstance(source_url, str) and source_url.strip():
                        sources.append(source_url.strip())

        deduped_snippets: list[str] = []
        seen: set[str] = set()
        for snippet in snippets:
            key = snippet.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped_snippets.append(snippet)

        if not deduped_snippets:
            fallback = _fallback_search(query, num_results)
            urls = re.findall(r"https?://\S+", fallback)
            fallback_sources: list[str] = []
            seen_urls: set[str] = set()
            for url in urls:
                clean = url.strip().rstrip(")].,;")
                if clean in seen_urls:
                    continue
                seen_urls.add(clean)
                fallback_sources.append(clean)

            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=fallback,
                data={
                    "query": query,
                    "num_results": num_results,
                    "livecrawl": livecrawl,
                    "type": search_type,
                    "context_max_characters": context_max_characters,
                    "source": "duckduckgo_fallback",
                    "snippet_count": 0,
                    "sources": fallback_sources,
                    "fallback_reason": "No usable snippets returned by web_search_exa",
                },
            )

        output_parts: list[str] = []
        for index, snippet in enumerate(deduped_snippets[:5], start=1):
            output_parts.append(f"Snippet {index}:\n{snippet}")

        unique_sources: list[str] = []
        seen_source: set[str] = set()
        for source in sources:
            if source in seen_source:
                continue
            seen_source.add(source)
            unique_sources.append(source)

        if unique_sources:
            output_parts.append("Sources:\n" + "\n".join(f"- {url}" for url in unique_sources[:10]))

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="\n\n".join(output_parts),
            data={
                "query": query,
                "num_results": num_results,
                "livecrawl": livecrawl,
                "type": search_type,
                "context_max_characters": context_max_characters,
                "source": "exa_mcp_web_search_exa",
                "snippet_count": len(deduped_snippets),
                "sources": unique_sources,
            },
        )
