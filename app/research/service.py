"""Child research agent.

The parent decides that research is needed and delegates a task. This agent
searches and reads public pages, then returns a report. It does not read
local files, run commands, change permissions, or treat page text as instructions.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from app.permissions.engine import ALLOW
from app.research.audit import (
    begin_research_audit,
    deny_research_audit,
    finish_research_audit,
    provider_label,
)
from app.research.fetch import FetchError, PageDocument, PageFetcher, UnsupportedContent
from app.research.guard import DestinationRejected, validate_url
from app.research.limits import ResearchLimits, limits_from_settings
from app.research.providers import BraveSearchProvider, JsonSearchProvider, RateLimiter
from app.research.queries import research_queries
from app.research.report import build_report, dedupe_hits, rank_hits, select_hits
from app.tools.exceptions import SearchProviderError, ToolExecutionError, ToolPermissionError
from app.tools.web_search import DuckDuckGoSearchProvider, SearchHit

_RATE = "The search rate limit was reached."
_PROVIDER = "A search provider did not return results."
_TIMEOUT = "A search provider timed out."
_REJECTED = "A page address was rejected."
_SKIPPED = "A page was skipped because its content type is not supported."
_PAGE_TIMEOUT = "A page retrieval timed out."
_PAGE = "A page could not be retrieved."
_BUDGET = "The research content limit was reached."


class ResearchAgent:
    def __init__(self, providers, fetcher, limits: ResearchLimits | None = None, limiter: RateLimiter | None = None, clock=None) -> None:
        self.providers = list(providers)
        self.fetcher = fetcher
        self.limits = limits or limits_from_settings()
        self.limiter = limiter or RateLimiter(self.limits.rate_limit_per_minute)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, task: str, *, agent_id: str | None) -> dict:
        self._authorize(agent_id)
        task_id = uuid4().hex
        providers = provider_label([provider.name for provider in self.providers])
        begin_research_audit(task_id, agent_id, providers)
        try:
            report = await self._gather(task, task_id, agent_id)
        except asyncio.CancelledError:
            _finish_quietly(task_id, agent_id, providers, "cancelled")
            raise
        except ToolExecutionError:
            _finish_quietly(task_id, agent_id, providers, "failed")
            raise
        except Exception as exc:
            _finish_quietly(task_id, agent_id, providers, "failed")
            raise ToolExecutionError("The research request could not be completed.") from exc
        finish_research_audit(task_id, agent_id, providers, "completed")
        return report

    def _authorize(self, agent_id: str | None) -> None:
        from app.permissions.service import evaluate_request
        from app.tools.registry import get_tool_registry

        decision = evaluate_request(
            agent_id=agent_id,
            tool_name="research",
            action="read",
            resource_type="internet",
            resource_id=None,
            relative_path=None,
            registry=get_tool_registry(),
        )
        if decision.decision != ALLOW:
            deny_research_audit(agent_id, decision.decision)
            raise ToolPermissionError("research")

    async def _gather(self, task: str, task_id: str, agent_id: str | None) -> dict:
        limitations: list[str] = []
        queries = research_queries(task, self.limits.max_queries)
        freshness = _fresh(task)
        collected: list[SearchHit] = []
        for query in queries:
            _checkpoint()
            for provider in self.providers:
                _checkpoint()
                collected.extend(await self._search(provider, query, limitations))
        usable = [hit for hit in collected if _public_hit(hit, limitations)]
        ranked = rank_hits(
            dedupe_hits(usable),
            now=self.clock(),
            freshness=freshness,
        )[: self.limits.max_results]
        selected = select_hits(ranked, self.limits.max_pages)
        pages = await self._pages(selected, limitations)
        return build_report(
            task_id=task_id,
            hits=selected,
            pages=pages,
            limitations=limitations,
            requesting_agent_id=agent_id,
            retrieved_at=self.clock().isoformat(),
        )

    async def _search(self, provider, query: str, limitations: list[str]) -> list[SearchHit]:
        if not self.limiter.allow(provider.name):
            _add(limitations, _RATE)
            return []
        attempts = self.limits.retries + 1
        for attempt in range(attempts):
            try:
                hits = await asyncio.wait_for(
                    provider.search(
                        query,
                        limit=self.limits.max_results,
                        timeout=self.limits.timeout_seconds,
                    ),
                    self.limits.timeout_seconds,
                )
            except SearchProviderError as exc:
                if exc.rate_limited:
                    _add(limitations, _RATE)
                    return []
                if attempt + 1 == attempts:
                    _add(limitations, _PROVIDER)
                    return []
                continue
            except TimeoutError:
                if attempt + 1 == attempts:
                    _add(limitations, _TIMEOUT)
                    return []
                continue
            if not isinstance(hits, list):
                _add(limitations, _PROVIDER)
                return []
            return [hit for hit in hits if isinstance(hit, SearchHit)][: self.limits.max_results]
        return []

    async def _pages(self, hits: list[SearchHit], limitations: list[str]) -> list[PageDocument]:
        semaphore = asyncio.Semaphore(self.limits.max_concurrent)

        async def one(hit: SearchHit):
            _checkpoint()
            try:
                validate_url(hit.url)
            except DestinationRejected:
                return None, _REJECTED
            try:
                async with semaphore:
                    _checkpoint()
                    page = await asyncio.wait_for(
                        self.fetcher.fetch(hit.url),
                        self.limits.timeout_seconds,
                    )
            except DestinationRejected:
                return None, _REJECTED
            except UnsupportedContent:
                return None, _SKIPPED
            except TimeoutError:
                return None, _PAGE_TIMEOUT
            except FetchError:
                return None, _PAGE
            if not isinstance(page, PageDocument):
                return None, _PAGE
            if page.published_at is None and hit.published_at:
                page = replace(page, published_at=hit.published_at)
            return page, None

        loaded = await asyncio.gather(*(one(hit) for hit in hits))
        pages: list[PageDocument] = []
        total = 0
        for page, problem in loaded:
            if problem:
                _add(limitations, problem)
                continue
            if page is None or total >= self.limits.max_total_bytes:
                _add(limitations, _BUDGET)
                break
            size = len(page.text.encode("utf-8"))
            if total + size > self.limits.max_total_bytes:
                page = _clip(page, self.limits.max_total_bytes - total)
                _add(limitations, _BUDGET)
            total += len(page.text.encode("utf-8"))
            pages.append(page)
        return pages


def default_research_agent() -> ResearchAgent:
    from app.core.config import get_settings

    settings = get_settings()
    providers = []
    if settings.web_search_provider == "duckduckgo":
        providers.append(DuckDuckGoSearchProvider())
    brave_key = settings.web_search_brave_key.strip()
    if brave_key:
        providers.append(BraveSearchProvider(brave_key, endpoint=settings.web_search_brave_url))
    endpoint = settings.web_search_secondary_url.strip()
    if endpoint:
        providers.append(JsonSearchProvider(endpoint, settings.web_search_secondary_key))
    return ResearchAgent(providers=providers, fetcher=PageFetcher())


def _checkpoint() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


def _public_hit(hit: SearchHit, limitations: list[str]) -> bool:
    try:
        validate_url(hit.url)
    except DestinationRejected:
        _add(limitations, _REJECTED)
        return False
    return True


def _clip(page: PageDocument, max_bytes: int) -> PageDocument:
    if max_bytes <= 0:
        return replace(page, text="", truncated=True)
    clipped = page.text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return replace(page, text=clipped, truncated=True)


def _fresh(task: str) -> bool:
    lowered = task.casefold()
    return any(word in lowered for word in ("latest", "today", "news", "recent", "current"))


def _add(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def _finish_quietly(task_id: str, agent_id: str | None, providers: str, status: str) -> None:
    try:
        finish_research_audit(task_id, agent_id, providers, status)
    except ToolExecutionError:
        return
