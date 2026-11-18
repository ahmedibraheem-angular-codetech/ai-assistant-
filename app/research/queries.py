"""Search queries taken from the task text. No unrelated query is added."""


def research_queries(task: str, limit: int) -> list[str]:
    base = " ".join(task.split())[:300]
    if not base or limit < 1:
        return []
    lowered = base.casefold()
    queries = [base]
    if _has(lowered, ("latest", "today", "news", "recent", "current")):
        _add(queries, f"{base} news")
    if _has(lowered, ("documentation", "docs", "api", "reference")):
        _add(queries, f"{base} documentation")
    if _has(lowered, ("price", "product", "service", "buy", "review")):
        _add(queries, f"{base} review")
    if _has(lowered, ("company", "startup", "technology")):
        _add(queries, f"{base} company")
    if len(queries) == 1 and limit > 1:
        words = [word for word in base.split() if len(word) > 3][:6]
        narrowed = " ".join(words)
        if narrowed and narrowed.casefold() != base.casefold():
            _add(queries, narrowed)
    return queries[:limit]


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _add(queries: list[str], query: str) -> None:
    if query not in queries:
        queries.append(query)
