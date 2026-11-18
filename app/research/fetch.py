"""Fetch one public page and return its text.

The connection target is the address that just passed validation. Redirects
are not followed by the HTTP client. Each hop is validated and resolved again.
"""

import asyncio
import gzip
import io
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import SplitResult

from app.research.extract import extract_html
from app.research.guard import DestinationRejected, literal_ip, next_url, require_public, validate_url
from app.research.limits import ResearchLimits, limits_from_settings

_ALLOWED = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_HEADER_CAP = 16_384


class FetchError(Exception):
    def __init__(self, message: str = "The page could not be retrieved.") -> None:
        super().__init__(message)


class UnsupportedContent(FetchError):
    def __init__(self) -> None:
        super().__init__("That page is not readable text.")


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool


@dataclass(frozen=True, slots=True)
class PageDocument:
    url: str
    final_url: str
    title: str
    canonical_url: str | None
    text: str
    content_type: str
    published_at: str | None
    retrieved_at: str
    truncated: bool
    data_boundary: str = "untrusted_web_content"


class PageFetcher:
    def __init__(
        self,
        *,
        resolver=None,
        connector=None,
        limits: ResearchLimits | None = None,
        clock=None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.limits = limits or limits_from_settings()
        self.resolver = resolver or system_resolver
        self.connector = connector or _connector_for(self.limits, ssl_context)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def fetch(self, url: str) -> PageDocument:
        current = url.strip()
        seen: set[str] = set()
        for hop in range(self.limits.max_redirects + 1):
            if current in seen:
                raise DestinationRejected()
            seen.add(current)
            parsed = validate_url(current)
            ip = await self._address(parsed.hostname or "")
            try:
                response = await asyncio.wait_for(
                    self.connector(parsed, ip),
                    self.limits.timeout_seconds,
                )
            except TimeoutError as exc:
                raise FetchError("The page retrieval timed out.") from exc
            if not isinstance(response, RawResponse):
                raise FetchError()
            if response.status in _REDIRECTS:
                if hop == self.limits.max_redirects:
                    raise FetchError("The page redirected too many times.")
                location = response.headers.get("location", "")
                current = next_url(current, location)
                continue
            if response.status != 200:
                raise FetchError()
            return self._document(url.strip(), current, response)
        raise FetchError("The page redirected too many times.")

    async def _address(self, host: str) -> str:
        literal = literal_ip(host)
        if literal is not None:
            return require_public([str(literal)])
        addresses = await self.resolver(host)
        if not isinstance(addresses, list):
            raise DestinationRejected()
        return require_public([str(item) for item in addresses])

    def _document(self, requested: str, final: str, response: RawResponse) -> PageDocument:
        if _attachment(response.headers):
            raise UnsupportedContent()
        media = _media_type(response.headers.get("content-type", ""))
        if media not in _ALLOWED:
            raise UnsupportedContent()
        body = response.body[: self.limits.max_page_bytes]
        truncated = response.truncated or len(response.body) > self.limits.max_page_bytes
        extracted = extract_html(
            body.decode("utf-8", errors="replace"),
            page_url=final,
            limit=self.limits.max_page_bytes,
        )
        retrieved = self.clock().isoformat()
        return PageDocument(
            url=requested,
            final_url=final,
            title=extracted.title or _host_title(final),
            canonical_url=extracted.canonical_url,
            text=extracted.text,
            content_type=media,
            published_at=extracted.published_at,
            retrieved_at=retrieved,
            truncated=truncated,
        )


def request_headers(parsed: SplitResult) -> list[tuple[str, str]]:
    host = parsed.hostname or ""
    if parsed.port and parsed.port not in {80, 443}:
        host = f"{host}:{parsed.port}"
    return [
        ("Host", host),
        ("User-Agent", "PersonalAIAssistant/research"),
        ("Accept", "text/html,text/plain,application/xhtml+xml"),
        ("Accept-Encoding", "identity"),
        ("Connection", "close"),
    ]


def parse_http_message(raw: bytes, max_bytes: int) -> RawResponse:
    if len(raw) > _HEADER_CAP + max_bytes:
        raw = raw[: _HEADER_CAP + max_bytes]
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator or len(head) > _HEADER_CAP:
        raise FetchError()
    lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise FetchError()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    truncated = False
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body, truncated = _chunks(body, max_bytes)
    else:
        if len(body) > max_bytes:
            body = body[:max_bytes]
            truncated = True
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > len(body):
            truncated = True
    encoding = headers.get("content-encoding", "").strip().lower()
    if "gzip" in encoding:
        body, gzip_truncated = _gunzip(body, max_bytes)
        truncated = truncated or gzip_truncated
    elif encoding and encoding != "identity":
        raise UnsupportedContent()
    return RawResponse(int(parts[1]), headers, body, truncated)


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
            5,
        )
    except (socket.gaierror, TimeoutError, OSError) as exc:
        raise DestinationRejected() from exc
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    return addresses


def verifying_context(context: ssl.SSLContext | None = None) -> ssl.SSLContext:
    """Use a context that checks the certificate and the hostname.

    A caller may add a private trust anchor for a test. A context that turns
    verification off is refused.
    """
    chosen = ssl.create_default_context() if context is None else context
    if chosen.verify_mode != ssl.CERT_REQUIRED or not chosen.check_hostname:
        raise FetchError("TLS certificate verification is required.")
    return chosen


def _connector_for(limits: ResearchLimits, ssl_context: ssl.SSLContext | None = None):
    async def connector(parsed: SplitResult, ip: str) -> RawResponse:
        return await _pinned_exchange(
            parsed,
            ip,
            timeout=limits.timeout_seconds,
            max_bytes=limits.max_page_bytes,
            ssl_context=ssl_context,
        )

    return connector


async def _pinned_exchange(
    parsed: SplitResult,
    ip: str,
    *,
    timeout: float,
    max_bytes: int,
    ssl_context: ssl.SSLContext | None = None,
) -> RawResponse:
    """Connect to ``ip`` only. The hostname is used for SNI, certificate checks, and Host."""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    context = None
    server_hostname = None
    if parsed.scheme == "https":
        context = verifying_context(ssl_context)
        server_hostname = parsed.hostname
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                ip,
                port,
                ssl=context,
                server_hostname=server_hostname,
                family=family,
            ),
            timeout,
        )
        payload = _request_bytes(parsed)
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout)
        raw = await asyncio.wait_for(_reader.read(_HEADER_CAP + max_bytes), timeout)
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        raise FetchError() from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    return parse_http_message(raw, max_bytes)


def _request_bytes(parsed: SplitResult) -> bytes:
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    lines = [f"GET {path} HTTP/1.1"]
    lines.extend(f"{name}: {value}" for name, value in request_headers(parsed))
    try:
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    except UnicodeError as exc:
        raise FetchError() from exc


def _chunks(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
    output = bytearray()
    cursor = 0
    truncated = False
    while cursor < len(data) and len(output) < max_bytes:
        line_end = data.find(b"\r\n", cursor)
        if line_end < 0:
            truncated = True
            break
        size_text = data[cursor:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise FetchError() from exc
        if size == 0:
            break
        if size < 0:
            raise FetchError()
        cursor = line_end + 2
        room = max_bytes - len(output)
        piece = data[cursor : cursor + size]
        if size > room:
            output.extend(piece[:room])
            truncated = True
            break
        if len(piece) < size:
            output.extend(piece)
            truncated = True
            break
        output.extend(piece)
        cursor += size
        if data[cursor : cursor + 2] == b"\r\n":
            cursor += 2
    return bytes(output[:max_bytes]), truncated


def _gunzip(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
            body = handle.read(max_bytes + 1)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise FetchError() from exc
    return body[:max_bytes], len(body) > max_bytes


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _attachment(headers: dict[str, str]) -> bool:
    disposition = headers.get("content-disposition", "").lower()
    return disposition.startswith("attachment")


def _host_title(url: str) -> str:
    parsed = validate_url(url)
    return (parsed.hostname or "page").removeprefix("www.")
