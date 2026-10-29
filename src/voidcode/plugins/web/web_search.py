from __future__ import annotations

import base64
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import ClassVar, final
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import OutputBounds, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess

logger = logging.getLogger(__name__)


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str
    numResults: int = Field(default=8, ge=1, le=20)

    @field_validator("query", mode="after")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value


TIMEOUT_SECONDS = 30
EMPTY_MESSAGE = "No results found for this query."
DEEPSEEK_MAX_QUERY_CHARS = 128
FIRECRAWL_DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"


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
    source: str
    results: tuple[WebSearchItem, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "query": self.query,
            "num_results": self.num_results,
            "source": self.source,
            "results": [item.as_payload() for item in self.results],
        }


def _render_search_results(results: list[WebSearchItem]) -> str:
    output_lines: list[str] = []
    for result in results:
        output_lines.extend((f"{result.rank}. {result.title}", f"   {result.url}"))
        if result.snippet:
            output_lines.append(f"   {result.snippet}")
        output_lines.append("")
    return "\n".join(output_lines)


def _bounded_http_error(label: str, exc: httpx.HTTPError) -> str:
    """One bounded failure sentence per transport fault; raw details stay in the log."""
    if isinstance(exc, httpx.TimeoutException):
        logger.warning("%s search timed out", label, exc_info=True)
        return f"{label} search timed out."
    if isinstance(exc, httpx.ConnectError):
        logger.warning("%s search connection failed", label, exc_info=True)
        return f"{label} search could not reach the search API."
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        logger.warning("%s search HTTP %s", label, status, exc_info=True)
        return f"{label} search failed with HTTP {status}."
    logger.warning("%s search request failed", label, exc_info=True)
    return f"{label} search request failed."


def _post_json(url: str, payload: Mapping[str, object], headers: Mapping[str, str], label: str) -> tuple[object, str | None]:
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.post(url, json=dict(payload), headers=dict(headers))
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return None, _bounded_http_error(label, exc)
    return _decode_json(response, label)


def _get_json(url: str, params: Mapping[str, str | int], headers: Mapping[str, str], label: str) -> tuple[object, str | None]:
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.get(url, params=dict(params), headers=dict(headers))
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return None, _bounded_http_error(label, exc)
    return _decode_json(response, label)


def _decode_json(response: httpx.Response, label: str) -> tuple[object, str | None]:
    try:
        return response.json(), None
    except ValueError:
        return None, f"{label} search returned an invalid response."


def _collect(
    raw_results: object, num_results: int, *, label: str, title_keys: tuple[str, ...] = ("title",), snippet_keys: tuple[str, ...] = ()
) -> tuple[list[WebSearchItem], str | None]:
    if not isinstance(raw_results, list):
        return [], f"{label} search returned an invalid response."
    results: list[WebSearchItem] = []
    for result in raw_results[:num_results]:
        if not isinstance(result, Mapping):
            continue
        url = result.get("url")
        if not isinstance(url, str) or not url:
            continue
        title: object = None
        for key in title_keys:
            candidate = result.get(key)
            if isinstance(candidate, str) and candidate:
                title = candidate
                break
        if title is None:
            title = url
        snippet = ""
        for key in snippet_keys:
            candidate = result.get(key)
            if isinstance(candidate, str) and candidate:
                snippet = candidate
                break
        results.append(WebSearchItem(len(results) + 1, title, url, snippet))
    return results, None


def _search_exa(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("EXA_API_KEY", "")
    data, error = _post_json(
        "https://api.exa.ai/search",
        {"query": query, "numResults": num_results, "type": "auto", "contents": {"highlights": True}},
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        "Exa",
    )
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "Exa search returned an invalid response."
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return [], "Exa search returned an invalid response."
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
    return results, None


def _search_tavily(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("TAVILY_API_KEY", "")
    data, error = _post_json(
        "https://api.tavily.com/search",
        {"query": query, "max_results": num_results},
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        "Tavily",
    )
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "Tavily search returned an invalid response."
    return _collect(data.get("results"), num_results, label="Tavily", snippet_keys=("content",))


def _search_brave(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("BRAVE_API_KEY", "")
    data, error = _get_json(
        "https://api.search.brave.com/res/v1/web/search",
        {"q": query, "count": num_results},
        {"Accept": "application/json", "X-Subscription-Token": api_key},
        "Brave",
    )
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "Brave search returned an invalid response."
    web_data = data.get("web")
    if not isinstance(web_data, Mapping):
        return [], "Brave search returned an invalid response."
    return _collect(web_data.get("results"), num_results, label="Brave", snippet_keys=("description",))


def _search_deepseek(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    data, error = _post_json(
        "https://api.deepseek.com/v1/tools/search",
        {"query": query[:DEEPSEEK_MAX_QUERY_CHARS], "count": num_results},
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        "DeepSeek",
    )
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "DeepSeek search returned an invalid response."
    return _collect(data.get("results"), num_results, label="DeepSeek", snippet_keys=("snippet",))


def _search_tinyfish(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("TINYFISH_API_KEY", "")
    data, error = _get_json(
        "https://api.search.tinyfish.ai",
        {"query": query},
        {"Accept": "application/json", "X-API-Key": api_key},
        "TinyFish",
    )
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "TinyFish search returned an invalid response."
    return _collect(data.get("results"), num_results, label="TinyFish", title_keys=("title", "site_name"), snippet_keys=("snippet",))


def _search_jina(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("JINA_API_KEY", "")
    headers = {"Accept": "application/json", "X-Respond-With": "no-content", "X-Retain-Images": "none"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data, error = _get_json(f"https://s.jina.ai/{quote(query, safe='')}", {"count": num_results}, headers, "Jina")
    if error is not None:
        return [], error
    if isinstance(data, list):
        raw_results: object = data
    elif isinstance(data, Mapping):
        code = data.get("code")
        if isinstance(code, int) and code != 200:
            return [], f"Jina search reported failure code {code}."
        raw_results = data.get("data")
    else:
        return [], "Jina search returned an invalid response."
    return _collect(raw_results, num_results, label="Jina", snippet_keys=("description", "content"))


def _search_kagi(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("KAGI_API_KEY", "")
    data, error = _post_json(
        "https://kagi.com/api/v1/search",
        {"query": query, "workflow": "search", "limit": num_results},
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        "Kagi",
    )
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "Kagi search returned an invalid response."
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
        return [], "Kagi search returned an invalid response."
    return _collect(
        raw_results,
        num_results,
        label="Kagi",
        title_keys=("title", "name"),
        snippet_keys=("snippet", "description", "summary"),
    )


def _firecrawl_endpoint() -> str:
    base_url = (os.environ.get("FIRECRAWL_BASE_URL") or os.environ.get("FIRECRAWL_API_URL") or FIRECRAWL_DEFAULT_BASE_URL).rstrip("/")
    if base_url.endswith("/v2") or base_url.endswith("/v1"):
        return f"{base_url}/search"
    return f"{base_url}/v2/search"


def _is_firecrawl_available() -> bool:
    return bool(os.environ.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_BASE_URL") or os.environ.get("FIRECRAWL_API_URL"))


def _search_firecrawl(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data, error = _post_json(
        _firecrawl_endpoint(),
        {"query": query, "limit": num_results, "sources": [{"type": "web"}]},
        headers,
        "Firecrawl",
    )
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "Firecrawl search returned an invalid response."
    if data.get("success") is False:
        return [], "Firecrawl search reported failure."
    raw_data = data.get("data")
    if isinstance(raw_data, list):
        raw_results: object = raw_data
    elif isinstance(raw_data, Mapping) and isinstance(raw_data.get("web"), list):
        raw_results = raw_data.get("web")
    elif isinstance(data.get("results"), list):
        raw_results = data.get("results")
    else:
        return [], "Firecrawl search returned an invalid response."
    return _collect(raw_results, num_results, label="Firecrawl", snippet_keys=("description", "snippet", "markdown"))


def _search_searxng(query: str, num_results: int) -> tuple[list[WebSearchItem], str | None]:
    endpoint = os.environ.get("SEARXNG_ENDPOINT", "").rstrip("/")
    headers = {"Accept": "application/json"}
    basic_user = os.environ.get("SEARXNG_BASIC_USERNAME")
    basic_pass = os.environ.get("SEARXNG_BASIC_PASSWORD")
    if basic_user and basic_pass:
        headers["Authorization"] = f"Basic {base64.b64encode(f'{basic_user}:{basic_pass}'.encode()).decode()}"
    elif token := os.environ.get("SEARXNG_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    data, error = _get_json(f"{endpoint}/search", {"q": query, "format": "json"}, headers, "SearXNG")
    if error is not None:
        return [], error
    if not isinstance(data, Mapping):
        return [], "SearXNG search returned an invalid response."
    return _collect(data.get("results"), num_results, label="SearXNG", snippet_keys=("content", "snippet"))


@dataclass(frozen=True, slots=True)
class SearchProvider:
    id: str
    is_available: Callable[[], bool]
    search: Callable[[str, int], tuple[list[WebSearchItem], str | None]]


def _has_env(name: str) -> bool:
    return bool(os.environ.get(name))


PROVIDERS: tuple[SearchProvider, ...] = (
    SearchProvider("exa", lambda: _has_env("EXA_API_KEY"), _search_exa),
    SearchProvider("tavily", lambda: _has_env("TAVILY_API_KEY"), _search_tavily),
    SearchProvider("brave", lambda: _has_env("BRAVE_API_KEY"), _search_brave),
    SearchProvider("deepseek", lambda: _has_env("DEEPSEEK_API_KEY"), _search_deepseek),
    SearchProvider("tinyfish", lambda: _has_env("TINYFISH_API_KEY"), _search_tinyfish),
    SearchProvider("kagi", lambda: _has_env("KAGI_API_KEY"), _search_kagi),
    SearchProvider("firecrawl", _is_firecrawl_available, _search_firecrawl),
    SearchProvider("searxng", lambda: _has_env("SEARXNG_ENDPOINT"), _search_searxng),
    # Jina answers keyless at low volume, so it is the last resort when nothing else has a credential.
    SearchProvider("jina", lambda: True, _search_jina),
)


@final
class WebSearchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="web_search",
        icon=Icon(glyph="⌕"),
        description="Search the web for information. Returns search results with titles, URLs, and snippets.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "numResults": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Number of results to return (default: 8)"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.NETWORK}),
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        _ = context, services
        args = WebSearchArgs.model_validate(dict(call.arguments))
        num_results = args.numResults
        provider = next(candidate for candidate in PROVIDERS if candidate.is_available())
        results, error = provider.search(args.query, num_results)
        if error is not None:
            return ToolFailure(
                tool_name=self.definition.name,
                error=error,
                output=TextOutput(f"Search source: {provider.id}\n\n{error}", bounds=OutputBounds(source=provider.id)),
                body=WebSearchResultBody(args.query, num_results, provider.id, ()),
                timeout_seconds=TIMEOUT_SECONDS,
            )
        rendered = _render_search_results(results) if results else EMPTY_MESSAGE
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(
                f"Search source: {provider.id}\n\n{rendered}",
                presentation=f"Found web results for {args.query} using {provider.id}.",
                bounds=OutputBounds(source=provider.id),
            ),
            body=WebSearchResultBody(args.query, num_results, provider.id, tuple(results)),
        )
