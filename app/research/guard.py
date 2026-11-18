"""Public http(s) destinations only.

Validation is a function the caller must run. A model argument cannot skip it.
"""

import ipaddress
from urllib.parse import urljoin, urlsplit

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet")


class DestinationRejected(Exception):
    def __init__(self) -> None:
        super().__init__("That address is not allowed.")


def validate_url(url: str):
    """Return the split URL when it is a public http(s) address."""
    if not isinstance(url, str):
        raise DestinationRejected()
    candidate = url.strip()
    if not candidate or len(candidate) > 2048:
        raise DestinationRejected()
    if any(character in candidate for character in "\r\n\t\x00 "):
        raise DestinationRejected()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise DestinationRejected()
    if parsed.username is not None or parsed.password is not None:
        raise DestinationRejected()
    host = parsed.hostname
    if not isinstance(host, str) or not host:
        raise DestinationRejected()
    host = host.rstrip(".").lower()
    if "%" in host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise DestinationRejected()
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise DestinationRejected() from exc
    literal = literal_ip(host)
    if literal is not None and not literal.is_global:
        raise DestinationRejected()
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise DestinationRejected()
    return parsed


def literal_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host.startswith("0x") or host.isdigit():
        raise DestinationRejected()
    labels = host.split(".")
    if 1 < len(labels) < 4 and all(label.isdigit() for label in labels):
        raise DestinationRejected()
    return None


def require_public(addresses: list[str]) -> str:
    """Accept one public address. Any private, reserved, or mixed record is rejected."""
    if not addresses:
        raise DestinationRejected()
    chosen: str | None = None
    for item in addresses:
        try:
            ip = ipaddress.ip_address(item)
        except ValueError as exc:
            raise DestinationRejected() from exc
        if not ip.is_global:
            raise DestinationRejected()
        if chosen is None:
            chosen = str(ip)
    if chosen is None:
        raise DestinationRejected()
    return chosen


def next_url(current: str, location: str) -> str:
    if not isinstance(location, str) or not location.strip():
        raise DestinationRejected()
    if any(character in location for character in "\r\n\x00"):
        raise DestinationRejected()
    joined = urljoin(current, location.strip())
    validate_url(joined)
    return joined
