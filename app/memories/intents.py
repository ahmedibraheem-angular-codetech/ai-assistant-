"""Deterministic memory-command detection.

Ordinary chat, hypothetical examples, and commands that appear only inside
quotes stay normal chat. The model never classifies these intents.
"""

import re
from dataclasses import dataclass
from enum import Enum

_QUOTED = re.compile(r"(?P<q>[\"“”'])(?P<body>.*?)(?P=q)", re.DOTALL)
_PREFIX = re.compile(
    r"^(?:please\s+|kindly\s+|can you\s+|could you\s+|would you\s+|i want you to\s+|i'd like you to\s+|i would like you to\s+)+",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"^(?:do not|don't|never)\s+(?:remember|forget|disable|enable|stop using)\b", re.IGNORECASE)
_REMEMBER = re.compile(r"^remember(?:\s+that|:)\s*(?P<fact>.*)$", re.IGNORECASE)
_FORGET_THAT = re.compile(r"^forget(?:\s+that)\s*(?P<subject>.*)$", re.IGNORECASE)
_FORGET_MY = re.compile(r"^forget\s+my\s+(?P<subject>.+)$", re.IGNORECASE)
_DELETE = re.compile(
    r"^(?:delete|remove)\s+(?:the\s+|my\s+)?(?:saved\s+)?memory(?:\s+(?:about|for|of))?\s+(?P<subject>.+)$",
    re.IGNORECASE,
)
_LIST_ABOUT = re.compile(r"^what do you remember(?:\s+about\s+(?P<topic>.+))?$", re.IGNORECASE)
_SHOW_SAVED = re.compile(r"^show my saved\s+(?P<topic>.+)$", re.IGNORECASE)
_LIST_MEMORIES = re.compile(r"^list my\s+(?P<topic>.+?)\s+memories$", re.IGNORECASE)
_LIST_ALL = re.compile(r"^(?:show|list) my memories$", re.IGNORECASE)
_DISABLE = re.compile(r"^(?:temporarily\s+stop using|stop using|disable)\s+(?P<subject>.+)$", re.IGNORECASE)
_ENABLE = re.compile(
    r"^(?:enable\s+(?P<subject>.+)|(?:start using|use)\s+(?P<subject_again>.+?)\s+again)$",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"[.?!]+$")
_TOKEN = re.compile(r"[a-z0-9]+")
_VAGUE = frozenset({"it", "that", "this", "something", "stuff", "them", "those", "one", "memory", "fact"})
_CONFIRM = frozenset(
    {
        "yes",
        "yes please",
        "confirm",
        "confirmed",
        "approve",
        "approved",
        "yes, save it",
        "yes, delete it",
        "yes, disable it",
        "yes, enable it",
        "go ahead",
    }
)
_CANCEL = frozenset(
    {
        "cancel",
        "cancel that",
        "never mind",
        "nevermind",
        "no",
        "no thanks",
        "do not save",
        "don't save",
        "no, cancel",
    }
)
_CATEGORIES = {
    "preference": "preferences",
    "preferences": "preferences",
    "project": "projects",
    "projects": "projects",
    "goal": "goals",
    "goals": "goals",
    "professional": "professional",
    "work": "professional",
    "personal": "personal",
    "custom": "custom",
}
_ALL_TOPICS = frozenset({"me", "myself", "us", "my life", "everything"})
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
_CATEGORY_HINTS = {
    "projects": {"project", "projects", "codebase", "repository", "repo", "stack"},
    "goals": {"goal", "goals", "objective"},
    "professional": {"work", "job", "office", "client", "company", "colleague"},
    "preferences": {"prefer", "preference", "preferences", "like", "dislike", "hate", "favorite", "favourite", "rather"},
}


class MemoryIntent(str, Enum):
    remember = "remember"
    forget = "forget"
    list_memories = "list_memories"
    disable_memory = "disable_memory"
    enable_memory = "enable_memory"
    normal_chat = "normal_chat"


@dataclass(frozen=True, slots=True)
class ParsedMemoryIntent:
    intent: MemoryIntent
    subject: str | None = None
    category: str | None = None
    list_all: bool = False
    ambiguous: bool = False
    clarification: str | None = None


def is_confirmation(message: str) -> bool:
    return _phrase(message) in _CONFIRM


def is_cancellation(message: str) -> bool:
    return _phrase(message) in _CANCEL


def parse_memory_intent(message: str) -> ParsedMemoryIntent:
    original = " ".join(message.split())
    unquoted = _QUOTED.sub(lambda match: " " * len(match.group(0)), original)
    prefix = _PREFIX.match(unquoted)
    if prefix is not None:
        original = original[prefix.end() :]
        unquoted = unquoted[prefix.end() :]
    if not unquoted.strip() or _NEGATED.match(unquoted):
        return _normal()

    remember = _REMEMBER.match(unquoted)
    if remember is not None:
        fact = _subject(original, remember)
        if not fact or _folded(fact) in _VAGUE:
            return _clarification(
                MemoryIntent.remember,
                "What should I remember? Say it as: Remember that ...",
            )
        return ParsedMemoryIntent(intent=MemoryIntent.remember, subject=fact)

    forget_that = _FORGET_THAT.match(unquoted)
    if forget_that is not None:
        subject = _subject(original, forget_that)
        if not subject or _folded(subject) in _VAGUE:
            return _clarification(
                MemoryIntent.forget,
                "Which saved fact should I forget? Name the specific memory.",
            )
        return ParsedMemoryIntent(intent=MemoryIntent.forget, subject=subject)

    for pattern, intent in (
        (_FORGET_MY, MemoryIntent.forget),
        (_DELETE, MemoryIntent.forget),
        (_DISABLE, MemoryIntent.disable_memory),
        (_ENABLE, MemoryIntent.enable_memory),
    ):
        match = pattern.match(unquoted)
        if match is None:
            continue
        subject = _subject(original, match)
        if not subject or _folded(subject) in _VAGUE:
            if intent is MemoryIntent.forget:
                return _normal()
            return _clarification(intent, "Which saved memory do you mean?")
        return ParsedMemoryIntent(intent=intent, subject=subject)

    if _LIST_ALL.match(unquoted):
        return ParsedMemoryIntent(intent=MemoryIntent.list_memories, list_all=True)
    listed = _LIST_ABOUT.match(unquoted) or _SHOW_SAVED.match(unquoted) or _LIST_MEMORIES.match(unquoted)
    if listed is not None:
        topic = listed.groupdict().get("topic")
        if topic is None:
            return ParsedMemoryIntent(intent=MemoryIntent.list_memories, list_all=True)
        topic = _TRAILING.sub("", topic).strip()
        category, list_all = _topic(topic)
        if list_all:
            return ParsedMemoryIntent(intent=MemoryIntent.list_memories, list_all=True)
        if category is None:
            if listed.re is _LIST_ABOUT:
                return _normal()
            return _clarification(
                MemoryIntent.list_memories,
                "I can list personal, professional, preferences, goals, projects, or custom memories.",
            )
        return ParsedMemoryIntent(intent=MemoryIntent.list_memories, category=category, subject=topic)
    return _normal()


def suggest_memory_fields(fact: str) -> dict[str, object]:
    content = _TRAILING.sub("", " ".join(fact.split())).strip()
    tokens = [token for token in _TOKEN.findall(content.casefold()) if len(token) >= 3 and token not in _STOPWORDS]
    key = " ".join(tokens[:6]) or "saved fact"
    return {
        "key": key[:100],
        "content": content[:4_000],
        "category": _infer_category(tokens),
        "description": "Explicitly saved by the user.",
        "tags": tokens[:5],
        "enabled": True,
        "priority": 0,
    }


def _topic(topic: str) -> tuple[str | None, bool]:
    cleaned = _folded(topic)
    cleaned = re.sub(r"^(my|the|our)\s+", "", cleaned)
    if cleaned in _ALL_TOPICS:
        return None, True
    return _CATEGORIES.get(cleaned), False


def _infer_category(tokens: list[str]) -> str:
    token_set = set(tokens)
    for category, hints in _CATEGORY_HINTS.items():
        if token_set & hints:
            return category
    return "personal"


def _subject(original: str, match: re.Match[str]) -> str:
    name = next((key for key, value in match.groupdict().items() if value is not None), None)
    if name is None:
        return ""
    text = original[match.start(name) : match.end(name)]
    return _TRAILING.sub("", " ".join(text.split())).strip()


def _phrase(message: str) -> str:
    text = " ".join(message.split()).casefold()
    return _TRAILING.sub("", text).strip()


def _folded(value: str) -> str:
    return " ".join(value.casefold().split())


def _normal() -> ParsedMemoryIntent:
    return ParsedMemoryIntent(intent=MemoryIntent.normal_chat)


def _clarification(intent: MemoryIntent, message: str) -> ParsedMemoryIntent:
    return ParsedMemoryIntent(intent=intent, ambiguous=True, clarification=message)
