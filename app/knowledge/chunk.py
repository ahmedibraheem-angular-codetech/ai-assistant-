"""Split extracted text into bounded chunks with optional location."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Segment:
    text: str
    page_number: int | None
    section: str | None


@dataclass(frozen=True, slots=True)
class Chunk:
    text: str
    page_number: int | None
    section: str | None
    ordinal: int


def chunk_segments(segments: list[Segment], *, size: int, overlap: int, limit: int) -> list[Chunk]:
    """Window the text. A later identical chunk is not stored again."""
    if overlap < 0 or overlap >= size:
        raise ValueError("Chunk overlap must be smaller than the chunk size.")
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for segment in segments:
        body = segment.text.strip()
        if not body:
            continue
        start = 0
        while start < len(body):
            end = min(len(body), start + size)
            if end < len(body):
                newline = body.rfind("\n", start + size // 2, end)
                if newline > start:
                    end = newline
            piece = body[start:end].strip()
            key = " ".join(piece.split())
            if piece and key not in seen:
                seen.add(key)
                chunks.append(
                    Chunk(
                        text=piece,
                        page_number=segment.page_number,
                        section=segment.section,
                        ordinal=len(chunks),
                    )
                )
                if len(chunks) > limit:
                    raise ValueError("That document has too many chunks.")
            if end >= len(body):
                break
            next_start = end - overlap
            if next_start <= start:
                next_start = end
            if next_start >= len(body):
                break
            start = next_start
    return chunks
