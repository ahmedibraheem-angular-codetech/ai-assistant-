"""Readable text from HTML. Script and style contents are not returned."""

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.research.guard import DestinationRejected, validate_url

_SKIP = frozenset({"script", "style", "noscript", "template", "iframe", "svg"})
_BREAKS = frozenset({"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"})
_PUBLISHED = frozenset({"article:published_time", "pubdate", "date", "dc.date"})


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    title: str
    text: str
    canonical_url: str | None
    published_at: str | None


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.canonical: str | None = None
        self.published: str | None = None
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, empty=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, empty=True)

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], *, empty: bool) -> None:
        name = tag.lower()
        if name in _SKIP:
            if not empty:
                self._skip += 1
            return
        if self._skip:
            return
        mapping = {key.lower(): value or "" for key, value in attrs if key}
        if name == "title":
            self._in_title = True
        if name == "link" and "canonical" in mapping.get("rel", "").lower():
            href = mapping.get("href", "").strip()
            if href:
                self.canonical = href
        if name == "meta" and self.published is None:
            label = (mapping.get("property") or mapping.get("name") or "").lower()
            if label in _PUBLISHED:
                content = " ".join(mapping.get("content", "").split())
                if content:
                    self.published = content[:40]
        if name in _BREAKS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _SKIP and self._skip:
            self._skip -= 1
            return
        if name == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)

    def handle_comment(self, data: str) -> None:
        return


def extract_html(markup: str, *, page_url: str, limit: int) -> ExtractedPage:
    parser = _Extractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    title = " ".join("".join(parser.title_parts).split())[:200]
    text = " ".join("".join(parser.text_parts).split())[:limit]
    return ExtractedPage(
        title=title,
        text=text,
        canonical_url=_canonical(page_url, parser.canonical),
        published_at=parser.published,
    )


def _canonical(page_url: str, href: str | None) -> str | None:
    if not href:
        return None
    try:
        joined = urljoin(page_url, href)
        validate_url(joined)
    except DestinationRejected:
        return None
    return joined
