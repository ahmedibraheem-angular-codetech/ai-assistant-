"""External web search. This is network access, not local model inference.

The built-in provider calls DuckDuckGo's instant-answer API. It does not fetch
the pages listed in the results and it does not scrape arbitrary websites.
"""

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.tools.base import ASYNC_READ_ONLY, Tool
from app.tools.exceptions import SearchProviderError, ToolExecutionError

_UNAVAILABLE = "Web search did not return results."


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    source: str
    published_at: str | None

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "published_at": self.published_at,
        }


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int, timeout: float) -> list[SearchHit]:
        """Return public results or raise SearchProviderError."""


class DuckDuckGoSearchProvider:
    """DuckDuckGo instant answers. No API key. Not a full web index."""

    name = "duckduckgo"
    endpoint = "https://api.duckduckgo.com/"

    async def search(self, query: str, *, limit: int, timeout: float) -> list[SearchHit]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    self.endpoint,
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": "1",
                        "skip_disambig": "1",
                        "no_redirect": "1",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchProviderError() from exc
        if not isinstance(payload, dict):
            raise SearchProviderError()
        return parse_duckduckgo(payload, limit=limit)


def parse_duckduckgo(payload: dict, *, limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    abstract_url = payload.get("AbstractURL")
    abstract = payload.get("AbstractText") or payload.get("Abstract") or ""
    if isinstance(abstract_url, str) and isinstance(abstract, str) and abstract.strip():
        source = payload.get("AbstractSource")
        title = payload.get("Heading")
        hit = _hit(
            title if isinstance(title, str) and title.strip() else None,
            abstract,
            abstract_url,
            source if isinstance(source, str) and source.strip() else None,
        )
        if hit is not None:
            hits.append(hit)
    _walk(payload.get("RelatedTopics"), hits, limit)
    return hits[:limit]


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search public web pages through an external search provider. "
        "This sends the query over the network and is not local model inference. "
        "The built-in provider is DuckDuckGo's instant-answer API, which is not a full web index."
    )
    category = "research"
    requires_confirmation = False
    execution_policy = ASYNC_READ_ONLY
    timeout_seconds = None
    enabled = True
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300},
        },
    }

    def __init__(self, provider: SearchProvider) -> None:
        self.provider = provider
        self.calls = 0

    def is_enabled(self) -> bool:
        settings = get_settings()
        return bool(
            self.enabled
            and settings.web_search_enabled
            and settings.web_search_provider == "duckduckgo"
        )

    async def run(self, arguments: dict) -> dict:
        self.calls += 1
        settings = get_settings()
        if not settings.web_search_enabled or settings.web_search_provider != "duckduckgo":
            raise ToolExecutionError("Web search is not available.")
        timeout = self.timeout_seconds or settings.tool_default_timeout_seconds
        try:
            hits = await self.provider.search(
                str(arguments["query"]),
                limit=settings.web_search_max_results,
                timeout=timeout,
            )
        except SearchProviderError as exc:
            raise ToolExecutionError(_UNAVAILABLE) from exc
        return {
            "query": arguments["query"],
            "provider": "duckduckgo",
            "network": "external",
            "results": [hit.as_dict() for hit in hits],
        }


def _walk(topics: object, hits: list[SearchHit], limit: int, depth: int = 0) -> None:
    if depth > 3 or len(hits) >= limit or not isinstance(topics, list):
        return
    for topic in topics:
        if len(hits) >= limit:
            return
        if not isinstance(topic, dict):
            continue
        nested = topic.get("Topics")
        if isinstance(nested, list):
            _walk(nested, hits, limit, depth + 1)
            continue
        url = topic.get("FirstURL")
        text = topic.get("Text")
        if not isinstance(url, str) or not isinstance(text, str):
            continue
        title, snippet = _split_topic(text)
        hit = _hit(title, snippet, url, None)
        if hit is not None:
            hits.append(hit)


def _split_topic(text: str) -> tuple[str, str]:
    if " - " in text:
        title, snippet = text.split(" - ", 1)
        return title.strip(), snippet.strip()
    stripped = text.strip()
    return stripped, stripped


def _hit(title: str | None, snippet: str, url: str, source: str | None) -> SearchHit | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    domain = parsed.hostname.removeprefix("www.")
    clean_title = (title or source or domain).strip()[:200]
    clean_snippet = " ".join(snippet.split())[:300]
    if not clean_title or not clean_snippet:
        return None
    return SearchHit(
        title=clean_title,
        url=url.strip(),
        snippet=clean_snippet,
        source=(source or domain).strip()[:200],
        published_at=None,
    )
