"""Opt-in live checks. Normal pytest runs skip this module before any network call.

Set ASSISTANT_LIVE_RESEARCH=1 to run it. A Brave token is read from
WEB_SEARCH_BRAVE_KEY when a live search is also requested. No token is written here.
"""

import ipaddress
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSISTANT_LIVE_RESEARCH") != "1",
    reason="Set ASSISTANT_LIVE_RESEARCH=1 to run the live research smoke test.",
)


@pytest.mark.asyncio
async def test_live_page_uses_the_validated_address_and_verifies_tls(monkeypatch):
    import asyncio

    from app.research.fetch import PageFetcher
    from app.research.limits import ResearchLimits

    connected: list[str] = []
    real_open = asyncio.open_connection

    async def recording(host, *args, **kwargs):
        connected.append(str(host))
        return await real_open(host, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", recording)
    fetcher = PageFetcher(
        limits=ResearchLimits(
            max_queries=1,
            max_results=1,
            max_pages=1,
            max_page_bytes=200_000,
            max_total_bytes=200_000,
            timeout_seconds=15,
            max_redirects=3,
            max_concurrent=1,
            retries=0,
            rate_limit_per_minute=5,
        )
    )
    secure = await fetcher.fetch("https://example.com/")
    assert secure.final_url == "https://example.com/"
    assert "Example Domain" in secure.text or "Example Domain" in secure.title
    assert secure.data_boundary == "untrusted_web_content"
    redirect = await fetcher.fetch(
        "https://httpbin.org/redirect-to?url=https://example.com/&status_code=302"
    )
    assert redirect.final_url == "https://example.com/"
    assert "Example Domain" in redirect.text or "Example Domain" in redirect.title
    assert len(connected) >= 2
    for host in connected:
        assert host not in {"example.com", "httpbin.org"}
        address = ipaddress.ip_address(host)
        assert address.is_global


@pytest.mark.asyncio
async def test_live_search_when_a_brave_key_is_configured():
    key = os.environ.get("WEB_SEARCH_BRAVE_KEY", "").strip()
    if not key:
        pytest.skip("WEB_SEARCH_BRAVE_KEY is not set, so live search was not called.")
    from app.research.providers import BraveSearchProvider

    hits = await BraveSearchProvider(key).search("python documentation", limit=3, timeout=15)
    assert hits
    assert all(item.url.startswith("https://") and item.title for item in hits)
    assert key not in " ".join(item.snippet for item in hits)
