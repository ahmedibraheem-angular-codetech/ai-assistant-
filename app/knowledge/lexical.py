"""Keyword matching for hybrid search, with Arabic-aware normalization.

Vector search finds text with similar meaning; it can miss exact names, codes,
and numbers. Keyword overlap catches those. The final score mixes both.
"""

from __future__ import annotations

import re
import unicodedata

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)  # letters and digits only (drops ؟ ، and _)
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # harakat + tatweel
_ARABIC_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
})
_STOP = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "what", "how",
    "في", "من", "على", "الى", "الي", "عن", "و", "او", "هو", "هي", "ده", "دي", "اي", "ايه", "ما", "هل", "اللي",
})
VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _DIACRITICS.sub("", text)
    return text.translate(_ARABIC_MAP)


def _strip_article(token: str) -> str:
    # Arabic definite article: "المشروع" should match "مشروع".
    if len(token) > 4 and token.startswith("ال"):
        return token[2:]
    return token


def terms(text: str) -> set[str]:
    found = set()
    for token in _TOKEN.findall(normalize(text)):
        token = _strip_article(token)
        if token and token not in _STOP and (len(token) > 1 or token.isdigit()):
            found.add(token)
    return found


def keyword_score(query_terms: set[str], text: str) -> float:
    """Share of query terms that appear in the text (0..1)."""
    if not query_terms:
        return 0.0
    present = terms(text)
    hits = sum(1 for term in query_terms if term in present)
    return hits / len(query_terms)


def combine(vector_score: float, keyword: float) -> float:
    return (VECTOR_WEIGHT * vector_score) + (KEYWORD_WEIGHT * keyword)
