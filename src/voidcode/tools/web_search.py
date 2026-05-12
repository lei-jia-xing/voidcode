from __future__ import annotations

import os
import re
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from ._pydantic_args import WebSearchArgs, format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult

DEFAULT_NUM_RESULTS = 8
DEFAULT_TIMEOUT = 30
DUCKDUCKGO_EMPTY_MESSAGE = "No search results found. Please try a different query."


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
) -> tuple[str, str, str | None]:
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

        results = _extract_duckduckgo_results(soup, num_results)

        if results:
            return _format_duckduckgo_results(results), "duckduckgo", None

        return (
            DUCKDUCKGO_EMPTY_MESSAGE,
            "duckduckgo-empty",
            "duckduckgo fallback returned no parseable results",
        )

    except Exception:
        return (
            DUCKDUCKGO_EMPTY_MESSAGE,
            "duckduckgo-error",
            "duckduckgo fallback failed before parsing results",
        )


def _extract_duckduckgo_results(
    soup: BeautifulSoup, num_results: int
) -> list[tuple[str, str, str]]:
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


def _format_duckduckgo_results(results: list[tuple[str, str, str]]) -> str:
    output_lines: list[str] = []
    for i, (title, result_url, snippet) in enumerate(results, 1):
        output_lines.append(f"{i}. {title}")
        output_lines.append(f"   {result_url}")
        if snippet:
            clean_snippet = re.sub(r"<[^>]+>", "", snippet)
            output_lines.append(f"   {clean_snippet[:200]}...")
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
        if (
            parsed.scheme
            and parsed.netloc
            and parsed.netloc.endswith("duckduckgo.com")
            and parsed.path.startswith("/l/")
        ):
            return raw_url
        if raw_url.startswith("/l/"):
            return raw_url
        return raw_url
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
            fallback_reason = None
        else:
            output, source, fallback_reason = _search_fallback(args.query, num_results, timeout)

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
            fallback_reason=fallback_reason,
        )
