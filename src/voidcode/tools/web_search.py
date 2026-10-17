from __future__ import annotations

import base64
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import parse_qs, quote, unquote, urlparse

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


def _search_tavily(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return None, "tavily API key not configured"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.tavily.com/search",
                json={"query": query, "max_results": num_results},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "tavily search returned an invalid response"
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return None, "tavily search returned an invalid response"
        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            title = result.get("title")
            url = result.get("url")
            content = result.get("content", "")
            snippet = content if isinstance(content, str) else ""
            if not isinstance(title, str) or not title or not isinstance(url, str) or not url:
                continue
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "tavily search returned no results")
    except Exception as exc:
        return None, f"tavily search failed: {exc}"


def _search_brave(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        return None, "brave API key not configured"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": num_results},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "brave search returned an invalid response"
        web_data = data.get("web")
        if not isinstance(web_data, Mapping):
            return None, "brave search returned an invalid response"
        raw_results = web_data.get("results")
        if not isinstance(raw_results, list):
            return None, "brave search returned an invalid response"
        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            title = result.get("title")
            url = result.get("url")
            description = result.get("description", "")
            snippet = description if isinstance(description, str) else ""
            if not isinstance(title, str) or not title or not isinstance(url, str) or not url:
                continue
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "brave search returned no results")
    except Exception as exc:
        return None, f"brave search failed: {exc}"


def _search_deepseek(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None, "deepseek API key not configured"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.deepseek.com/v1/tools/search",
                json={"query": query, "count": num_results},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "deepseek search returned an invalid response"
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return None, "deepseek search returned an invalid response"
        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            title = result.get("title")
            url = result.get("url")
            snippet = result.get("snippet", "")
            snippet = snippet if isinstance(snippet, str) else ""
            if not isinstance(title, str) or not title or not isinstance(url, str) or not url:
                continue
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "deepseek search returned no results")
    except Exception as exc:
        return None, f"deepseek search failed: {exc}"


def _search_tinyfish(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("TINYFISH_API_KEY")
    if not api_key:
        return None, "tinyfish API key not configured"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                "https://api.search.tinyfish.ai",
                params={"query": query, "num_results": num_results},
                headers={"Accept": "application/json", "X-API-Key": api_key},
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "tinyfish search returned an invalid response"
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return None, "tinyfish search returned an invalid response"
        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or result.get("site_name") or url
            if not isinstance(title, str) or not title:
                continue
            snippet = result.get("snippet", "")
            snippet = snippet if isinstance(snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "tinyfish search returned no results")
    except Exception as exc:
        return None, f"tinyfish search failed: {exc}"


def _search_jina(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("JINA_API_KEY")
    if not api_key:
        return None, "jina API key not configured"

    try:
        quoted_query = quote(query, safe="")
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                f"https://s.jina.ai/{quoted_query}",
                params={"count": num_results},
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "X-Respond-With": "no-content",
                    "X-Retain-Images": "none",
                },
            )
            response.raise_for_status()
            data = response.json()

        if isinstance(data, list):
            raw_results = data
        elif isinstance(data, Mapping):
            code = data.get("code")
            if isinstance(code, int) and code != 200:
                return None, f"jina search reported failure code {code}"
            raw_data = data.get("data")
            if isinstance(raw_data, list):
                raw_results = raw_data
            else:
                return None, "jina search returned an invalid response"
        else:
            return None, "jina search returned an invalid response"

        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or url
            if not isinstance(title, str) or not title:
                continue
            snippet = result.get("description") or result.get("content") or ""
            snippet = snippet if isinstance(snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "jina search returned no results")
    except Exception as exc:
        return None, f"jina search failed: {exc}"


def _search_kagi(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("KAGI_API_KEY")
    if not api_key:
        return None, "kagi API key not configured"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://kagi.com/api/v1/search",
                json={"query": query, "workflow": "search", "limit": num_results},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "kagi search returned an invalid response"
        content = data.get("data")
        raw_results: list[object] = []
        if isinstance(content, Mapping):
            for bucket in ("search", "video", "news", "infobox"):
                items = content.get(bucket)
                if isinstance(items, list):
                    raw_results.extend(items)
        elif isinstance(content, list):
            raw_results = content
        else:
            return None, "kagi search returned an invalid response"

        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url") or result.get("href") or result.get("link")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or result.get("name") or url
            if not isinstance(title, str) or not title:
                continue
            snippet = result.get("snippet") or result.get("description") or result.get("summary") or ""
            snippet = snippet if isinstance(snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "kagi search returned no results")
    except Exception as exc:
        return None, f"kagi search failed: {exc}"


def _search_firecrawl(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    base_url = (os.environ.get("FIRECRAWL_BASE_URL") or os.environ.get("FIRECRAWL_API_URL") or "https://api.firecrawl.dev/v2").rstrip("/")
    if not base_url.endswith("/v2") and not base_url.endswith("/v1"):
        endpoint = f"{base_url}/v2/search"
    else:
        endpoint = f"{base_url}/search"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                endpoint,
                json={"query": query, "limit": num_results, "sources": [{"type": "web"}]},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "firecrawl search returned an invalid response"
        if data.get("success") is False:
            error_msg = data.get("error") or "firecrawl request reported failure"
            return None, f"firecrawl search failed: {error_msg}"

        raw_data = data.get("data")
        if isinstance(raw_data, list):
            raw_results = raw_data
        elif isinstance(raw_data, Mapping) and isinstance(raw_data.get("web"), list):
            raw_results = raw_data.get("web")
        elif isinstance(data.get("results"), list):
            raw_results = data.get("results")
        else:
            return None, "firecrawl search returned an invalid response"

        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or url
            if not isinstance(title, str) or not title:
                continue
            snippet = result.get("description") or result.get("snippet") or result.get("markdown") or ""
            snippet = snippet if isinstance(snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "firecrawl search returned no results")
    except Exception as exc:
        return None, f"firecrawl search failed: {exc}"


def _search_kimi(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("KIMI_SEARCH_API_KEY") or os.environ.get("MOONSHOT_SEARCH_API_KEY")
    if not api_key:
        return None, "kimi API key not configured"

    endpoint = os.environ.get("MOONSHOT_SEARCH_BASE_URL") or os.environ.get("KIMI_SEARCH_BASE_URL") or "https://api.kimi.com/coding/v1/search"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                endpoint,
                json={"text_query": query, "limit": num_results},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "kimi search returned an invalid response"
        raw_results = data.get("search_results")
        if not isinstance(raw_results, list):
            return None, "kimi search returned an invalid response"

        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or result.get("site_name") or url
            if not isinstance(title, str) or not title:
                continue
            snippet = result.get("snippet") or result.get("content") or ""
            snippet = snippet if isinstance(snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "kimi search returned no results")
    except Exception as exc:
        return None, f"kimi search failed: {exc}"


def _search_parallel(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("PARALLEL_API_KEY")
    if not api_key:
        return None, "parallel API key not configured"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.parallel.ai/v1beta/search",
                json={
                    "objective": query,
                    "search_queries": [query],
                    "mode": "fast",
                    "excerpts": {"max_chars_per_result": 10000},
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "parallel-beta": "search-extract-2025-10-10",
                },
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "parallel search returned an invalid response"
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return None, "parallel search returned an invalid response"

        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or url
            if not isinstance(title, str) or not title:
                continue
            excerpts = result.get("excerpts")
            if isinstance(excerpts, list) and excerpts:
                snippet = "\n\n".join(str(e) for e in excerpts if e)
            else:
                raw_snippet = result.get("snippet", "")
                snippet = raw_snippet if isinstance(raw_snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "parallel search returned no results")
    except Exception as exc:
        return None, f"parallel search failed: {exc}"


def _search_synthetic(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    api_key = os.environ.get("SYNTHETIC_API_KEY")
    if not api_key:
        return None, "synthetic API key not configured"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.synthetic.new/v2/search",
                json={"query": query},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "synthetic search returned an invalid response"
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return None, "synthetic search returned an invalid response"

        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or url
            if not isinstance(title, str) or not title:
                continue
            snippet = result.get("text", "")
            snippet = snippet if isinstance(snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "synthetic search returned no results")
    except Exception as exc:
        return None, f"synthetic search failed: {exc}"


def _search_searxng(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    endpoint = os.environ.get("SEARXNG_ENDPOINT")
    if not endpoint:
        return None, "SEARXNG_ENDPOINT not configured"

    base_url = endpoint.rstrip("/")
    headers = {"Accept": "application/json"}
    basic_user = os.environ.get("SEARXNG_BASIC_USERNAME")
    basic_pass = os.environ.get("SEARXNG_BASIC_PASSWORD")
    if basic_user and basic_pass:
        auth_bytes = f"{basic_user}:{basic_pass}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(auth_bytes).decode()}"
    elif token := os.environ.get("SEARXNG_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                f"{base_url}/search",
                params={"q": query, "format": "json"},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, Mapping):
            return None, "searxng search returned an invalid response"
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            return None, "searxng search returned an invalid response"

        results: list[WebSearchItem] = []
        for result in raw_results[:num_results]:
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = result.get("title") or url
            if not isinstance(title, str) or not title:
                continue
            snippet = result.get("content") or result.get("snippet") or ""
            snippet = snippet if isinstance(snippet, str) else ""
            results.append(WebSearchItem(len(results) + 1, title, url, snippet))
        return (results, None) if results else (None, "searxng search returned no results")
    except Exception as exc:
        return None, f"searxng search failed: {exc}"


def _extract_startpage_results(soup: BeautifulSoup, num_results: int) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for container in soup.select("div.result"):
        anchor = container.select_one("a.result-link")
        if not anchor:
            continue
        href = anchor.get("href")
        if not isinstance(href, str) or not href:
            continue
        if not href.startswith(("http://", "https://")):
            continue
        parsed = urlparse(href)
        hostname = (parsed.hostname or "").lower()
        if hostname == "startpage.com" or hostname.endswith(".startpage.com"):
            continue

        heading = anchor.select_one("h2, h3")
        title = heading.get_text(" ", strip=True) if heading else anchor.get_text(" ", strip=True)
        if not title:
            continue

        desc = container.select_one("p.description")
        snippet = desc.get_text(" ", strip=True) if desc else ""

        results.append((title, href, snippet))
        if len(results) >= num_results:
            break
    return results


def _search_startpage(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                "https://www.startpage.com/sp/search",
                params={"query": query},
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; VoidCode/1.0)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            if response.status_code != 200:
                return None, f"startpage search failed with status {response.status_code}"
            text = response.text
            if "component---src-pages-captcha" in text or "/sp/captcha" in text or "/errors/" in str(response.url):
                return None, "startpage search was challenged or blocked: CAPTCHA required"
            soup = BeautifulSoup(text, "html.parser")

        extracted = _extract_startpage_results(soup, num_results)
        results = [WebSearchItem(rank, title, url, snippet) for rank, (title, url, snippet) in enumerate(extracted, 1)]
        return (results, None) if results else (None, "startpage search returned no results")
    except Exception as exc:
        return None, f"startpage search failed: {exc}"


def _search_duckduckgo(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[WebSearchItem] | None, str | None]:
    results, source, fallback_reason, error = _search_fallback(query, num_results, timeout)
    if error is not None:
        return None, fallback_reason
    if not results:
        return [], fallback_reason
    return results, None


@dataclass(frozen=True, slots=True)
class SearchProvider:
    id: str
    label: str
    is_available: Callable[[], bool]
    search: Callable[[str, int, int], tuple[list[WebSearchItem] | None, str | None]]


DEFAULT_PROVIDER_ORDER: tuple[str, ...] = (
    "exa",
    "tavily",
    "brave",
    "deepseek",
    "tinyfish",
    "jina",
    "kagi",
    "firecrawl",
    "kimi",
    "parallel",
    "synthetic",
    "searxng",
    "startpage",
    "duckduckgo",
)

SEARCH_PROVIDERS: dict[str, SearchProvider] = {
    "exa": SearchProvider(
        id="exa",
        label="Exa",
        is_available=lambda: bool(os.environ.get("EXA_API_KEY")),
        search=_search_exa,
    ),
    "tavily": SearchProvider(
        id="tavily",
        label="Tavily",
        is_available=lambda: bool(os.environ.get("TAVILY_API_KEY")),
        search=_search_tavily,
    ),
    "brave": SearchProvider(
        id="brave",
        label="Brave",
        is_available=lambda: bool(os.environ.get("BRAVE_API_KEY")),
        search=_search_brave,
    ),
    "deepseek": SearchProvider(
        id="deepseek",
        label="DeepSeek",
        is_available=lambda: bool(os.environ.get("DEEPSEEK_API_KEY")),
        search=_search_deepseek,
    ),
    "tinyfish": SearchProvider(
        id="tinyfish",
        label="TinyFish",
        is_available=lambda: bool(os.environ.get("TINYFISH_API_KEY")),
        search=_search_tinyfish,
    ),
    "jina": SearchProvider(
        id="jina",
        label="Jina",
        is_available=lambda: bool(os.environ.get("JINA_API_KEY")),
        search=_search_jina,
    ),
    "kagi": SearchProvider(
        id="kagi",
        label="Kagi",
        is_available=lambda: bool(os.environ.get("KAGI_API_KEY")),
        search=_search_kagi,
    ),
    "firecrawl": SearchProvider(
        id="firecrawl",
        label="Firecrawl",
        is_available=lambda: True,
        search=_search_firecrawl,
    ),
    "kimi": SearchProvider(
        id="kimi",
        label="Kimi",
        is_available=lambda: bool(os.environ.get("KIMI_SEARCH_API_KEY") or os.environ.get("MOONSHOT_SEARCH_API_KEY")),
        search=_search_kimi,
    ),
    "parallel": SearchProvider(
        id="parallel",
        label="Parallel",
        is_available=lambda: bool(os.environ.get("PARALLEL_API_KEY")),
        search=_search_parallel,
    ),
    "synthetic": SearchProvider(
        id="synthetic",
        label="Synthetic",
        is_available=lambda: bool(os.environ.get("SYNTHETIC_API_KEY")),
        search=_search_synthetic,
    ),
    "searxng": SearchProvider(
        id="searxng",
        label="SearXNG",
        is_available=lambda: bool(os.environ.get("SEARXNG_ENDPOINT")),
        search=_search_searxng,
    ),
    "startpage": SearchProvider(
        id="startpage",
        label="Startpage",
        is_available=lambda: True,
        search=_search_startpage,
    ),
    "duckduckgo": SearchProvider(
        id="duckduckgo",
        label="DuckDuckGo",
        is_available=lambda: True,
        search=_search_duckduckgo,
    ),
}


def _resolve_candidate_ids(provider_override: str | None = None) -> list[str]:
    if provider_override is not None:
        return [provider_override.strip().lower()]

    order_env = os.environ.get("VOIDCODE_WEB_SEARCH_ORDER")
    if order_env and order_env.strip():
        prioritized = [p for item in order_env.split(",") if (p := item.strip().lower()) in SEARCH_PROVIDERS]
        if prioritized:
            seen = set(prioritized)
            order = prioritized + [p for p in DEFAULT_PROVIDER_ORDER if p not in seen]
        else:
            order = list(DEFAULT_PROVIDER_ORDER)
    else:
        order = list(DEFAULT_PROVIDER_ORDER)

    exclude_env = os.environ.get("VOIDCODE_WEB_SEARCH_EXCLUDE", "")
    excluded = {item.strip().lower() for item in exclude_env.split(",") if item.strip()}
    return [p for p in order if p not in excluded]


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

    def invoke(self, call: ToolCall, *, context: ToolContext, provider: str | None = None) -> ToolResult:
        _ = context
        return self._invoke(call, runtime_timeout_seconds=None, provider=provider)

    def invoke_with_runtime_timeout(self, call: ToolCall, *, context: ToolContext, timeout_seconds: int, provider: str | None = None) -> ToolResult:
        _ = context
        return self._invoke(call, runtime_timeout_seconds=timeout_seconds, provider=provider)

    def _invoke(
        self,
        call: ToolCall,
        *,
        runtime_timeout_seconds: int | None,
        provider: str | None = None,
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

        candidate_ids = _resolve_candidate_ids(provider)

        if provider is not None:
            chosen = candidate_ids[0]
            if chosen not in SEARCH_PROVIDERS:
                error = f"Unknown web search provider: {provider!r}"
                provenance = "Search source: none"
                bounds = OutputBounds(source="none", fallback_reason=error)
                body = WebSearchResultBody(args.query, num_results, timeout, "none", error, ())
                return ToolFailure(
                    tool_name=self.definition.name,
                    error=error,
                    output=TextOutput(f"{provenance}\n\n{error}", bounds=bounds),
                    body=body,
                    timeout_seconds=timeout,
                )
            available_ids = candidate_ids
        else:
            available_ids = [p for p in candidate_ids if SEARCH_PROVIDERS[p].is_available()]

        if not available_ids:
            message = "No web search provider configured."
            provenance = "Search source: none"
            bounds = OutputBounds(source="none", fallback_reason=message)
            body = WebSearchResultBody(args.query, num_results, timeout, "none", message, ())
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(f"{provenance}\n\n{message}", bounds=bounds),
                body=body,
                timeout_seconds=timeout,
            )

        failures: list[str] = []
        winning_results: list[WebSearchItem] | None = None
        winning_source: str | None = None
        empty_success_source: str | None = None
        empty_success_reason: str | None = None
        last_provider_id: str | None = None

        for pid in available_ids:
            last_provider_id = pid
            prov = SEARCH_PROVIDERS[pid]
            res, err = prov.search(args.query, num_results, timeout)
            if res:
                winning_results = res
                winning_source = pid
                break
            if res == []:
                empty_success_source = f"{pid}-empty"
                empty_success_reason = err
                if err:
                    failures.append(err)
            elif err:
                failures.append(err)

        if winning_results:
            source = winning_source or "unknown"
            fallback_reason = "; ".join(failures) if failures else None
            output = _render_search_results(winning_results)
            provenance = f"Search source: {source}"
            if fallback_reason:
                provenance += f" | fallback: {fallback_reason}"
            output = f"{provenance}\n\n{output}"
            bounds = OutputBounds(source=source, fallback_reason=fallback_reason)
            body = WebSearchResultBody(args.query, num_results, timeout, source, fallback_reason, tuple(winning_results))
            return ToolSuccess(
                tool_name=self.definition.name,
                output=TextOutput(
                    output,
                    presentation=f"Found web results for {args.query} using {source}.",
                    bounds=bounds,
                ),
                body=body,
            )

        if empty_success_source is not None:
            source = empty_success_source
            fallback_reason = "; ".join(failures) if failures else empty_success_reason
            output = DUCKDUCKGO_EMPTY_MESSAGE
            provenance = f"Search source: {source}"
            if fallback_reason:
                provenance += f" | fallback: {fallback_reason}"
            output = f"{provenance}\n\n{output}"
            bounds = OutputBounds(source=source, fallback_reason=fallback_reason)
            body = WebSearchResultBody(args.query, num_results, timeout, source, fallback_reason, ())
            return ToolSuccess(
                tool_name=self.definition.name,
                output=TextOutput(
                    output,
                    presentation=f"Found web results for {args.query} using {source}.",
                    bounds=bounds,
                ),
                body=body,
            )

        last_id = last_provider_id or "web-search"
        source = f"{last_id}-error"
        fallback_reason = "; ".join(failures) if failures else "All web search providers failed"
        if len(failures) > 1:
            error_msg = f"All web search providers failed: {fallback_reason}"
        else:
            error_msg = failures[0] if failures else "Web search failed"

        provenance = f"Search source: {source}"
        if fallback_reason:
            provenance += f" | fallback: {fallback_reason}"
        output = f"{provenance}\n\n{error_msg}"
        bounds = OutputBounds(source=source, fallback_reason=fallback_reason)
        body = WebSearchResultBody(args.query, num_results, timeout, source, fallback_reason, ())
        return ToolFailure(
            tool_name=self.definition.name,
            error=error_msg,
            output=TextOutput(output, bounds=bounds),
            body=body,
            timeout_seconds=timeout,
        )
