from dataclasses import dataclass

from app.core.config import get_settings


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    max_queries: int
    max_results: int
    max_pages: int
    max_page_bytes: int
    max_total_bytes: int
    timeout_seconds: float
    max_redirects: int
    max_concurrent: int
    retries: int
    rate_limit_per_minute: int


def limits_from_settings() -> ResearchLimits:
    settings = get_settings()
    return ResearchLimits(
        max_queries=settings.research_max_queries,
        max_results=settings.web_search_max_results,
        max_pages=settings.research_max_pages,
        max_page_bytes=settings.research_max_page_bytes,
        max_total_bytes=settings.research_max_total_bytes,
        timeout_seconds=settings.research_timeout_seconds,
        max_redirects=settings.research_max_redirects,
        max_concurrent=settings.research_max_concurrent,
        retries=settings.research_retries,
        rate_limit_per_minute=settings.web_search_rate_limit_per_minute,
    )
