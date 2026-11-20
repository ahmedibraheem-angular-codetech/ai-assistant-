"""Turn one approved byte buffer into located text. The path is not opened."""

import json
from io import BytesIO

from app.files.exceptions import FileAccessError
from app.files.parsers import category_for, extract
from app.knowledge.chunk import Segment

_EMPTY = "The document has no text."
_UNREADABLE = "The file could not be read."


def segments_from_bytes(data: bytes, extension: str, *, output_chars: int) -> list[Segment]:
    from app.knowledge.ocr import IMAGE_EXTENSIONS, image_text

    if extension.lower() in IMAGE_EXTENSIONS:
        text = image_text(data, extension)[:output_chars]
        if not text.strip():
            raise FileAccessError("No text was found in that image.")
        return [Segment(text=text, page_number=None, section="image")]
    kind = category_for(extension)
    if kind == "pdf":
        return _pdf_segments(data, output_chars)
    body = extract(
        data,
        extension,
        output_chars=output_chars,
        rows=500,
        columns=30,
        json_depth=8,
    )
    text = _as_text(body)
    if not text.strip():
        raise FileAccessError(_EMPTY)
    if kind == "markdown":
        return _markdown_segments(text)
    section = None
    if kind == "spreadsheet":
        section = "sheet"
    return [Segment(text=text, page_number=None, section=section)]


def _pdf_segments(data: bytes, output_chars: int) -> list[Segment]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except Exception as exc:
        raise FileAccessError(_UNREADABLE) from exc
    if getattr(reader, "is_encrypted", False):
        raise FileAccessError("That PDF is encrypted and was not read.")
    segments: list[Segment] = []
    total = 0
    for index, page in enumerate(reader.pages, start=1):
        if total >= output_chars:
            break
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise FileAccessError(_UNREADABLE) from exc
        remaining = output_chars - total
        if len(text) > remaining:
            text = text[:remaining]
        total += len(text)
        if text.strip():
            segments.append(Segment(text=text, page_number=index, section=None))
    if not segments:
        raise FileAccessError(_EMPTY)
    return segments


def _markdown_segments(text: str) -> list[Segment]:
    sections: list[Segment] = []
    current = "document"
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if any(item.strip() for item in lines):
                sections.append(Segment(text="\n".join(lines), page_number=None, section=current))
            current = line.lstrip("#").strip()[:200] or current
            lines = []
            continue
        lines.append(line)
    if any(item.strip() for item in lines):
        sections.append(Segment(text="\n".join(lines), page_number=None, section=current))
    return sections or [Segment(text=text, page_number=None, section=None)]


def _as_text(body: dict) -> str:
    content = body.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, default=str)
    sheets = body.get("sheets")
    if isinstance(sheets, list):
        return json.dumps(sheets, default=str)
    rows = body.get("rows")
    if isinstance(rows, list):
        return json.dumps({"headers": body.get("headers"), "rows": rows}, default=str)
    return ""
