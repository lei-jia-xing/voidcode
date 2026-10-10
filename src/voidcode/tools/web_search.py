from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, field_validator

from ..core.tool_context import ToolContext
from ._pydantic_args import parse_tool_args, validate_non_empty
from .contracts import OutputBounds, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess


class WebSearchArgs(BaseModel):
    query: str
    numResults: int = 8

    _validate_query = field_validator("query", mode="after")(validate_non_empty)

    @field_validator("numResults", mode="after")
    @classmethod
    def _validate_num_results(cls, value: int) -> int:
        if value < 1:
            raise ValueError("numResults must be greater than or equal to 1")
        return value


DEFAULT_NUM_RESULTS = 8
DEFAULT_TIMEOUT = 30
DUCKDUCKGO_EMPTY_MESSAGE = "No search results found. Please try a different query."


@dataclass(frozen=True, slots=True)
class WebSearchItem:
    rank: int
    title: str
    url: str
    snippet: str

    def as_payload(self) -> dict[str, object]:
        return {"rank": self.rank, "title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass(frozen=True, slots=True)
class WebSearchResultBody:
    query: str
    num_results: int
    timeout_seconds: int
    source: str
    fallback_reason: str | None
    results: tuple[WebSearchItem, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "query": self.query,
            "num_results": self.num_results,
            "timeout_seconds": self.timeout_seconds,
            "source": self.source,
            "fallback_reason": self.fallback_reason,
            "results": [item.as_payload() for item in self.results],
        }


def _search_exa(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return None, None

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.exa.ai/search",
                json={"query": query, "numResults": num_results, "type": "auto", "contents": {"highlights": True}},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "exa search returned an invalid response"
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return None, "exa search returned an invalid response"
        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            title = result.get("title")
            url = result.get("url")
            highlights = result.get("highlights", [])
            snippet = " ".join(item for item in highlights if isinstance(item, str)) if isinstance(highlights, list) else ""
            if not isinstance(title, str) or not title or not isinstance(url, str) or not url:
                continue
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "exa search returned no results")
    except Exception as exc:
        return None, f"exa search failed: {exc}"


def _search_fallback(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem], str, str | None, str | None]:
    url = "https://html.duckduckgo.com/html/"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                url,
                params={"q": query, "kl": "wt-wt"},
                headers={"User-Agent": "Mozilla/5.0 (compatible; VoidCode/1.0)"},
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

        results = [
            WebSearchItem(rank, title, result_url, snippet)
            for rank, (title, result_url, snippet) in enumerate(_extract_duckduckgo_results(soup, num_results), 1)
        ]
        if results:
            return results, "duckduckgo", None, None
        return [], "duckduckgo-empty", "duckduckgo fallback returned no parseable results", None
    except Exception as exc:
        return [], "duckduckgo-error", f"duckduckgo fallback failed: {exc}", f"Web search failed: {exc}"


def _extract_duckduckgo_results(soup: BeautifulSoup, num_results: int) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for container in _duckduckgo_result_containers(soup):
        title, result_url = _duckduckgo_container_title_and_url(container)
        if not title or not result_url:
            continue

        dedupe_key = (title, result_url)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        snippet = _duckduckgo_container_snippet(container)
        results.append((title, result_url, snippet))

        if len(results) >= num_results:
            break

    if len(results) < num_results:
        for anchor in soup.select("a.result__a, a[href]"):
            title, result_url = _duckduckgo_anchor_title_and_url(anchor)
            if not title or not result_url:
                continue

            dedupe_key = (title, result_url)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            snippet = _duckduckgo_anchor_snippet(anchor)
            results.append((title, result_url, snippet))

            if len(results) >= num_results:
                break

    return results


def _duckduckgo_result_containers(soup: BeautifulSoup) -> list[Tag]:
    selectors = (
        "div.result",
        "div.result__body",
        'article[data-testid="result"]',
        'div[data-testid="result"]',
        'li[data-testid="result"]',
        "article",
    )

    containers: list[Tag] = []
    seen_ids: set[int] = set()

    for selector in selectors:
        for container in soup.select(selector):
            container_id = id(container)
            if container_id in seen_ids:
                continue
            seen_ids.add(container_id)
            containers.append(container)

    return containers


def _duckduckgo_container_title_and_url(container: Tag) -> tuple[str, str]:

    link_selectors = (
        "a.result__a",
        "h2 a[href]",
        "h3 a[href]",
        "a[href]",
    )

    for selector in link_selectors:
        for anchor in container.select(selector):
            title = anchor.get_text(" ", strip=True)
            raw_href = anchor.get("href")
            href = raw_href if isinstance(raw_href, str) else ""
            if not title or not href:
                continue

            result_url = _resolve_duckduckgo_result_url(href)
            if result_url:
                return title, result_url

    return "", ""


def _duckduckgo_anchor_title_and_url(anchor: Tag) -> tuple[str, str]:
    title = anchor.get_text(" ", strip=True)
    raw_href = anchor.get("href")
    href = raw_href if isinstance(raw_href, str) else ""
    if not title or not href:
        return "", ""

    result_url = _resolve_duckduckgo_result_url(href)
    if result_url:
        return title, result_url

    return "", ""


def _duckduckgo_container_snippet(container: Tag) -> str:

    snippet_selectors = (
        ".result__snippet",
        "[data-result-snippet]",
        ".snippet",
        ".exsnippet",
        ".result__body .snippet",
    )

    for selector in snippet_selectors:
        snippet_node = container.select_one(selector)
        if snippet_node is not None:
            return snippet_node.get_text(" ", strip=True)

    snippet_node = container.find(
        ["div", "span", "a", "p"],
        class_=re.compile("snippet|exsnippet|result__snippet"),
    )
    if snippet_node is not None:
        return snippet_node.get_text(" ", strip=True)

    return ""


def _duckduckgo_anchor_snippet(anchor: Tag) -> str:
    parent = anchor.parent
    if isinstance(parent, Tag):
        for sibling in parent.next_siblings:
            if not isinstance(sibling, Tag):
                continue
            if sibling.name == "a" and "result__a" in (sibling.get("class") or []):
                break
            if "snippet" in " ".join(sibling.get("class") or []):
                return sibling.get_text(" ", strip=True)
            candidate = sibling.find(
                ["div", "span", "a", "p"],
                class_=re.compile("snippet|exsnippet|result__snippet"),
            )
            if candidate is not None:
                return candidate.get_text(" ", strip=True)

    return ""


def _render_search_results(results: list[WebSearchItem]) -> str:
    output_lines: list[str] = []
    for result in results:
        output_lines.extend((f"{result.rank}. {result.title}", f"   {result.url}"))
        if result.snippet:
            output_lines.append(f"   {result.snippet}")
        output_lines.append("")
    return "\n".join(output_lines)


def _resolve_duckduckgo_result_url(raw_url: str) -> str | None:
    if raw_url.startswith("//"):
        return _resolve_duckduckgo_result_url(f"https:{raw_url}")
    if raw_url.startswith("http://") or raw_url.startswith("https://") or raw_url.startswith("/"):
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)
        for key in ("uddg", "u"):
            values = query.get(key)
            if values:
                return unquote(values[0])
        if parsed.scheme and parsed.netloc and parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            return raw_url
        if raw_url.startswith("/l/"):
            return raw_url
        return raw_url
    return None


class WebSearchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="web_search",
        description=("Search the web for information. Returns search results with titles, URLs, and snippets."),
        input_schema={
            "query": {"type": "string", "description": "The search query"},
            "numResults": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Number of results to return (default: 8)",
            },
            "required": ["query"],
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.NETWORK}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = context
        return self._invoke(call, runtime_timeout_seconds=None)

    def invoke_with_runtime_timeout(self, call: ToolCall, *, context: ToolContext, timeout_seconds: int) -> ToolResult:
        _ = context
        return self._invoke(call, runtime_timeout_seconds=timeout_seconds)

    def _invoke(
        self,
        call: ToolCall,
        *,
        runtime_timeout_seconds: int | None,
    ) -> ToolResult:
        args = parse_tool_args(
            WebSearchArgs,
            {
                "query": call.arguments.get("query"),
                "numResults": call.arguments.get("numResults", DEFAULT_NUM_RESULTS),
            },
            tool_name=self.definition.name,
        )

        num_results = min(args.numResults, 20)

        timeout = (
            DEFAULT_TIMEOUT
            if runtime_timeout_seconds is None
            else min(
                DEFAULT_TIMEOUT,
                runtime_timeout_seconds,
            )
        )

        exa_results, exa_failure = _search_exa(args.query, num_results, timeout)
        if exa_results:
            results = exa_results
            source = "exa"
            fallback_reason = None
            error = None
        else:
            results, source, fallback_reason, error = _search_fallback(args.query, num_results, timeout)
            fallback_reason = exa_failure or fallback_reason

        output = _render_search_results(results) if results else DUCKDUCKGO_EMPTY_MESSAGE
        if error is not None:
            output = error
        provenance = f"Search source: {source}"
        if fallback_reason:
            provenance += f" | fallback: {fallback_reason}"
        output = f"{provenance}\n\n{output}"
        bounds = OutputBounds(source=source, fallback_reason=fallback_reason)
        body = WebSearchResultBody(args.query, num_results, timeout, source, fallback_reason, tuple(results))
        if error is not None:
            return ToolFailure(
                tool_name=self.definition.name,
                error=error,
                output=TextOutput(output, bounds=bounds),
                body=body,
                timeout_seconds=timeout,
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(
                output,
                presentation=f"Found web results for {args.query} using {source}.",
                bounds=bounds,
            ),
            body=body,
        )
