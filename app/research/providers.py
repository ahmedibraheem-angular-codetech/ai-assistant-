"""Search providers used by the research agent.

Provider clients stay here. The agent sees normalized hits only.
DuckDuckGo's instant-answer API is included only when it is the selected
provider. It is not a full web index. Brave web search is included only when
WEB_SEARCH_BRAVE_KEY is set. A custom endpoint is included only when
WEB_SEARCH_SECONDARY_URL is set.
"""

import time
from urllib.parse import urlparse

import httpx

from app.tools.exceptions import SearchProviderError
from app.tools.web_search import SearchHit


class RateLimiter:
    """One sliding window per provider. A refused call is not sent."""

    def __init__(self, limit: int, clock=None) -> None:
        self.limit = limit
        self.clock = clock or time.monotonic
        self._hits: dict[str, list[float]] = {}

    def allow(self, name: str) -> bool:
        now = float(self.clock())
        window = [stamp for stamp in self._hits.get(name, []) if now - stamp < 60]
        if len(window) >= self.limit:
            self._hits[name] = window
            return False
        window.append(now)
        self._hits[name] = window
        return True


_BRAVE_WEB = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider:
    """Brave web search. The subscription token comes from configuration.

    Brave's published Search price is $5 per 1,000 requests. A monthly credit
    is part of a Brave plan, not a keyless free endpoint. This class does not
    store a token of its own.
    """

    name = "brave"

    def __init__(self, api_key: str, endpoint: str = _BRAVE_WEB, client=None) -> None:
        self.endpoint = endpoint or _BRAVE_WEB
        self._api_key = api_key
        self._client = client

    async def search(self, query: str, *, limit: int, timeout: float) -> list[SearchHit]:
        if not self._api_key.strip() or not self.endpoint.strip():
            raise SearchProviderError()
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {"q": query, "count": str(max(1, min(limit, 20)))}
        try:
            if self._client is not None:
                payload = await self._client(self.endpoint, params, headers, timeout)
            else:
                payload = await _fetch_brave(self.endpoint, params, headers, timeout)
        except SearchProviderError:
            raise
        except Exception as exc:
            raise SearchProviderError() from exc
        return parse_brave_results(payload, limit=limit)


def parse_brave_results(payload: object, *, limit: int) -> list[SearchHit]:
    if not isinstance(payload, dict):
        raise SearchProviderError()
    rows: list[object] = []
    for section in ("web", "news"):
        block = payload.get(section)
        if isinstance(block, dict) and isinstance(block.get("results"), list):
            rows.extend(block["results"])
    hits: list[SearchHit] = []
    for row in rows:
        if len(hits) >= limit:
            break
        if not isinstance(row, dict):
            continue
        published = row.get("page_age") or row.get("age")
        hit = _hit(
            {
                "title": row.get("title"),
                "url": row.get("url"),
                "snippet": row.get("description"),
                "source": _brave_source(row),
                "published_at": published,
            }
        )
        if hit is not None:
            hits.append(hit)
    return hits


def _brave_source(row: dict) -> str | None:
    profile = row.get("profile")
    if isinstance(profile, dict) and isinstance(profile.get("name"), str):
        return profile["name"]
    return None


async def _fetch_brave(endpoint: str, params: dict[str, str], headers: dict[str, str], timeout: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(endpoint, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise SearchProviderError(timed_out=True) from exc
    except httpx.HTTPError as exc:
        raise SearchProviderError() from exc
    if response.status_code == 429:
        raise SearchProviderError(rate_limited=True)
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchProviderError() from exc
    if not isinstance(payload, dict):
        raise SearchProviderError()
    return payload


class JsonSearchProvider:
    """JSON search endpoint configured by the operator. No key is built in."""

    name = "secondary"

    def __init__(self, endpoint: str, api_key: str = "", client=None) -> None:
        self.endpoint = endpoint
        self._api_key = api_key
        self._client = client

    async def search(self, query: str, *, limit: int, timeout: float) -> list[SearchHit]:
        if not self.endpoint.strip():
            raise SearchProviderError()
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        try:
            if self._client is not None:
                payload = await self._client(self.endpoint, query, headers, timeout)
            else:
                payload = await _fetch_json(self.endpoint, query, headers, timeout)
        except SearchProviderError:
            raise
        except Exception as exc:
            raise SearchProviderError() from exc
        return parse_configured_results(payload, limit=limit)


def parse_configured_results(payload: object, *, limit: int) -> list[SearchHit]:
    if not isinstance(payload, dict):
        raise SearchProviderError()
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise SearchProviderError()
    hits: list[SearchHit] = []
    for row in rows:
        if len(hits) >= limit:
            break
        if not isinstance(row, dict):
            continue
        hit = _hit(row)
        if hit is not None:
            hits.append(hit)
    return hits


async def _fetch_json(endpoint: str, query: str, headers: dict[str, str], timeout: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(endpoint, params={"q": query}, headers=headers)
    except httpx.TimeoutException as exc:
        raise SearchProviderError(timed_out=True) from exc
    except httpx.HTTPError as exc:
        raise SearchProviderError() from exc
    if response.status_code == 429:
        raise SearchProviderError(rate_limited=True)
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchProviderError() from exc
    if not isinstance(payload, dict):
        raise SearchProviderError()
    return payload


def _hit(row: dict) -> SearchHit | None:
    url = row.get("url")
    snippet = row.get("snippet")
    title = row.get("title")
    source = row.get("source")
    published = row.get("published_at")
    if not isinstance(url, str) or not isinstance(snippet, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    domain = parsed.hostname.removeprefix("www.")
    clean_title = title.strip()[:200] if isinstance(title, str) and title.strip() else domain
    clean_snippet = " ".join(snippet.split())[:300]
    if not clean_title or not clean_snippet:
        return None
    published_at = None
    if isinstance(published, str):
        published_at = " ".join(published.split())[:40] or None
    source_name = source.strip()[:200] if isinstance(source, str) and source.strip() else domain
    return SearchHit(
        title=clean_title,
        url=url.strip(),
        snippet=clean_snippet,
        source=source_name,
        published_at=published_at,
    )
