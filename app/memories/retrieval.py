import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from app.memories.models import Memory

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
        "you",
        "your",
    }
)


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    text: str
    agent_name: str | None = None
    agent_role: str | None = None


class MemoryRetriever(Protocol):
    """Select memories for a query.

    A later semantic retriever can replace KeywordMemoryRetriever without
    changing memory storage or the chat path.
    """

    def rank(self, memories: Sequence[Memory], query: MemoryQuery) -> list[Memory]:
        """Return matching memories, most relevant first."""


class KeywordMemoryRetriever:
    """Local matching on keys, keywords, categories, and tags."""

    def rank(self, memories: Sequence[Memory], query: MemoryQuery) -> list[Memory]:
        scored: list[tuple[int, Memory]] = []
        for memory in memories:
            score = _score(memory, query)
            if score > 0:
                scored.append((score, memory))
        scored.sort(key=lambda item: (item[0], item[1].priority, item[1].updated_at), reverse=True)
        return [memory for _, memory in scored]


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _tokenize(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text.casefold()) if len(token) >= 2 and token not in _STOPWORDS}


def _score(memory: Memory, query: MemoryQuery) -> int:
    query_text = " ".join(query.text.casefold().split())
    query_tokens = _tokenize(query.text)
    if not query_text:
        return 0

    score = 0
    key = " ".join(memory.key.casefold().split())
    key_tokens = _tokenize(memory.key)
    if key and _contains_phrase(query_text, key):
        score += 100
    if key_tokens and key_tokens <= query_tokens:
        score += 40

    for tag in memory.tags:
        folded = " ".join(tag.casefold().split())
        if folded and (_contains_phrase(query_text, folded) or _tokenize(tag) & query_tokens):
            score += 25

    if memory.category in query_tokens:
        score += 20

    field_tokens = key_tokens | _tokenize(memory.content) | _tokenize(memory.description or "")
    overlap = field_tokens & query_tokens
    score += 10 * len(overlap)

    if score <= 0:
        return 0

    context_tokens: set[str] = set()
    if query.agent_name:
        context_tokens |= _tokenize(query.agent_name)
    if query.agent_role:
        context_tokens |= _tokenize(query.agent_role)
    if field_tokens & context_tokens:
        score += 5
    return score + min(max(memory.priority, 0), 20)
