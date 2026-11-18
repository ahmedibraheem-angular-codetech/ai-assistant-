"""Structured research report built only from retrieved text.

A sentence repeated inside one domain family is one source. The same sentence
on two domain families is verified. Different numbers or polarity on the same
claim are a conflict. Text written by this module is labeled as analysis.
"""

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.research.fetch import PageDocument
from app.tools.web_search import SearchHit

_SENTENCE = re.compile(r"[^.!?]{12,240}[.!?]")
_DIGITS = re.compile(r"\d+")
_POLARITY = re.compile(r"\b(not|no|false|true)\b", re.IGNORECASE)


def normalize_result_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    base = f"{parsed.scheme.lower()}://{host}{path}"
    if parsed.query:
        return f"{base}?{parsed.query}"
    return base


def domain_family(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def rank_hits(hits: list[SearchHit], *, now: datetime, freshness: bool) -> list[SearchHit]:
    def sort_key(item: tuple[int, SearchHit]) -> tuple[float, int]:
        index, hit = item
        score = 2.0 if hit.url.lower().startswith("https://") else 0.0
        published = _published(hit.published_at)
        if published is not None:
            age_days = max(0.0, (now - published).total_seconds() / 86400)
            score += max(0.0, 30 - age_days) / 5
            if freshness:
                score += max(0.0, 14 - age_days)
        elif freshness:
            score -= 1
        return (-score, index)

    return [hit for _index, hit in sorted(enumerate(hits), key=sort_key)]


def select_hits(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    chosen: list[SearchHit] = []
    seen: set[str] = set()
    deferred: list[SearchHit] = []
    for hit in hits:
        if len(chosen) >= limit:
            break
        family = domain_family(hit.url)
        if family in seen:
            deferred.append(hit)
            continue
        chosen.append(hit)
        seen.add(family)
    for hit in deferred:
        if len(chosen) >= limit:
            break
        chosen.append(hit)
    return chosen


def dedupe_hits(hits: list[SearchHit]) -> list[SearchHit]:
    unique: list[SearchHit] = []
    seen: set[str] = set()
    for hit in hits:
        key = normalize_result_url(hit.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


def build_report(
    *,
    task_id: str,
    hits: list[SearchHit],
    pages: list[PageDocument],
    limitations: list[str],
    requesting_agent_id: str | None,
    retrieved_at: str,
) -> dict:
    pages_by_request = {page.url: page for page in pages}
    observations = _observations(hits, pages_by_request, retrieved_at)
    claims = _claims(observations)
    sources = _sources(hits, pages_by_request, retrieved_at)
    conflicts = [claim for claim in claims if claim["status"] == "conflicting"]
    findings = [claim["text"] for claim in claims if claim["status"] in {"verified", "single_source"}]
    summary = _summary(claims, sources)
    confidence = _confidence(claims)
    return {
        "task_id": task_id,
        "requesting_agent_id": requesting_agent_id,
        "research_role": "research",
        "summary": summary,
        "findings": findings,
        "claims": claims,
        "sources": sources,
        "conflicts": [
            {"text": claim["text"], "sources": claim["sources"], "evidence": claim["evidence"]}
            for claim in conflicts
        ],
        "confidence": confidence,
        "limitations": _unique(limitations),
        "analysis": {"label": "agent_generated_analysis", "text": summary},
        "data_boundary": "untrusted_web_content",
    }


def _observations(hits, pages_by_request, retrieved_at: str) -> list[dict]:
    rows: list[dict] = []
    for hit in hits:
        page = pages_by_request.get(hit.url)
        if page is None:
            snippet = " ".join(hit.snippet.split())
            if len(snippet) >= 12:
                rows.append(_observation(snippet, hit, hit.url, False, retrieved_at, hit.published_at))
            continue
        texts = _claim_texts(page.text)
        if not texts and len(page.text) >= 12:
            texts = [page.text[:240]]
        published = page.published_at or hit.published_at
        for text in texts:
            rows.append(_observation(text, hit, page.final_url, True, page.retrieved_at, published, page.title))
    return rows


def _observation(text, hit, url, from_page, retrieved_at, published, title=None) -> dict:
    return {
        "text": text,
        "url": url,
        "title": title or hit.title,
        "published_at": published,
        "retrieved_at": retrieved_at,
        "family": domain_family(url),
        "from_page": from_page,
        "key": _key(text),
        "digits": tuple(_DIGITS.findall(text)),
        "negative": _negative(text),
    }


def _claims(observations: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    unverified: list[dict] = []
    for item in observations:
        if not item["from_page"]:
            unverified.append(_one_claim(item, "unverified", [item]))
            continue
        key = item["key"]
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(item)
    claims = [_group_claim(grouped[key]) for key in order]
    claims.extend(unverified)
    return claims[:12]


def _group_claim(items: list[dict]) -> dict:
    families = {item["family"] for item in items}
    digits = {item["digits"] for item in items}
    polarity = {item["negative"] for item in items}
    texts = {item["text"].casefold() for item in items}
    if len(families) >= 2 and (len(digits) > 1 or len(polarity) > 1):
        status = "conflicting"
    elif len(families) >= 2 and len(texts) == 1:
        status = "verified"
    elif len(families) == 1:
        status = "single_source"
    else:
        status = "unverified"
    return _one_claim(items[0], status, items)


def _one_claim(primary: dict, status: str, items: list[dict]) -> dict:
    sources = _source_rows(items)
    evidence = [item["text"] for item in items]
    conflicts = [item["text"] for item in items[1:] if item["text"] != primary["text"]]
    return {
        "text": primary["text"],
        "status": status,
        "sources": sources,
        "evidence": evidence,
        "conflicts": conflicts,
    }


def _source_rows(items: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        rows.append(
            {
                "url": item["url"],
                "title": item["title"],
                "published_at": item["published_at"],
                "retrieved_at": item["retrieved_at"],
                "data_boundary": "untrusted_web_content",
            }
        )
    return rows


def _sources(hits, pages_by_request, retrieved_at: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        page = pages_by_request.get(hit.url)
        url = page.final_url if page is not None else hit.url
        if url in seen:
            continue
        seen.add(url)
        rows.append(
            {
                "url": url,
                "title": page.title if page is not None else hit.title,
                "published_at": (page.published_at if page is not None and page.published_at else hit.published_at),
                "retrieved_at": page.retrieved_at if page is not None else retrieved_at,
                "canonical_url": page.canonical_url if page is not None else None,
                "excerpt": (page.text[:280] if page is not None else hit.snippet[:280]),
                "data_boundary": "untrusted_web_content",
            }
        )
    return rows


def _claim_texts(text: str) -> list[str]:
    collapsed = " ".join(text.split())
    found: list[str] = []
    for match in _SENTENCE.finditer(collapsed):
        sentence = " ".join(match.group(0).split())
        if sentence not in found:
            found.append(sentence)
        if len(found) == 3:
            break
    return found


def _key(sentence: str) -> str:
    text = _POLARITY.sub(" ", sentence.casefold())
    text = _DIGITS.sub("#", text)
    text = re.sub(r"[^a-z# ]+", "", text)
    return " ".join(text.split())


def _negative(sentence: str) -> bool:
    return re.search(r"\b(not|no|false)\b", sentence.casefold()) is not None


def _published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _summary(claims: list[dict], sources: list[dict]) -> str:
    if not claims and not sources:
        return "The available sources do not establish an answer. Evidence is insufficient."
    if any(claim["status"] == "conflicting" for claim in claims):
        return "Sources disagree. Conflicting claims are separate from findings."
    if not any(claim["status"] in {"verified", "single_source"} for claim in claims):
        return "The available sources do not establish an answer. Evidence is insufficient."
    return "Findings are limited to text retrieved from the listed sources."


def _confidence(claims: list[dict]) -> str:
    if any(claim["status"] == "conflicting" for claim in claims):
        return "low"
    if any(claim["status"] == "verified" for claim in claims):
        return "high"
    if any(claim["status"] == "single_source" for claim in claims):
        return "medium"
    return "low"


def _unique(items: list[str]) -> list[str]:
    rows: list[str] = []
    for item in items:
        if item not in rows:
            rows.append(item)
    return rows
