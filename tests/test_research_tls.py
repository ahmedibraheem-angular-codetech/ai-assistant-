"""Local HTTPS proof for the pinned transport. No public network is used."""

import asyncio
import socket
import ssl
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.research.fetch import FetchError, PageFetcher, _pinned_exchange, verifying_context
from app.research.guard import DestinationRejected
from app.research.limits import ResearchLimits


def _openssl(directory: Path, *args: str) -> None:
    completed = subprocess.run(
        ["openssl", *args],
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)


def _certificate(directory: Path, name: str, hostname: str) -> tuple[Path, Path]:
    request_config = directory / f"{name}.cnf"
    request_config.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name=dn",
                "prompt=no",
                "[dn]",
                f"CN={hostname}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    extensions = directory / f"{name}-ext.cnf"
    extensions.write_text(
        "\n".join(
            [
                "[ext]",
                f"subjectAltName=DNS:{hostname}",
                "basicConstraints=CA:FALSE",
                "keyUsage=digitalSignature,keyEncipherment",
                "extendedKeyUsage=serverAuth",
                "subjectKeyIdentifier=hash",
                "authorityKeyIdentifier=keyid,issuer",
                "",
            ]
        ),
        encoding="utf-8",
    )
    key = directory / f"{name}.key"
    csr = directory / f"{name}.csr"
    cert = directory / f"{name}.pem"
    _openssl(
        directory,
        "req",
        "-newkey",
        "rsa:2048",
        "-keyout",
        key.name,
        "-out",
        csr.name,
        "-nodes",
        "-config",
        request_config.name,
    )
    _openssl(
        directory,
        "x509",
        "-req",
        "-in",
        csr.name,
        "-CA",
        "ca.pem",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        cert.name,
        "-days",
        "2",
        "-extfile",
        extensions.name,
        "-extensions",
        "ext",
    )
    return cert, key


def _authority(directory: Path) -> None:
    config = directory / "ca.cnf"
    config.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name=dn",
                "x509_extensions=v3_ca",
                "prompt=no",
                "[dn]",
                "CN=Research Test CA",
                "[v3_ca]",
                "basicConstraints=critical,CA:TRUE",
                "keyUsage=critical,keyCertSign,cRLSign",
                "subjectKeyIdentifier=hash",
                "authorityKeyIdentifier=keyid:always,issuer",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _openssl(
        directory,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "2",
        "-nodes",
        "-config",
        "ca.cnf",
    )


def _client_context(ca_file: Path) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(ca_file))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    return context


def _server_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


async def _serve(context: ssl.SSLContext, response: bytes):
    seen: list[tuple[tuple, bytes]] = []

    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")
        request = await reader.readuntil(b"\r\n\r\n")
        seen.append((peer, request))
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    return server, port, seen


def _limits() -> ResearchLimits:
    return ResearchLimits(
        max_queries=1,
        max_results=1,
        max_pages=1,
        max_page_bytes=20_000,
        max_total_bytes=20_000,
        timeout_seconds=3,
        max_redirects=1,
        max_concurrent=1,
        retries=0,
        rate_limit_per_minute=5,
    )


@pytest.fixture
def certificates(tmp_path):
    _authority(tmp_path)
    matching, matching_key = _certificate(tmp_path, "pin", "pin.research.test")
    other, other_key = _certificate(tmp_path, "other", "other.research.test")
    return {
        "ca": tmp_path / "ca.pem",
        "matching": matching,
        "matching_key": matching_key,
        "other": other,
        "other_key": other_key,
    }


def test_verification_cannot_be_turned_off():
    context = ssl.create_default_context()
    assert verifying_context(context) is context
    disabled = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    disabled.check_hostname = False
    disabled.verify_mode = ssl.CERT_NONE
    with pytest.raises(FetchError, match="verification"):
        verifying_context(disabled)


@pytest.mark.asyncio
async def test_pinned_https_accepts_a_matching_certificate(certificates, monkeypatch):
    looked_up: list[str] = []
    real_lookup = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        looked_up.append(str(host))
        if str(host) == "pin.research.test":
            raise AssertionError("the hostname was resolved after validation")
        return real_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
    body = b"<html><title>Pinned</title><p>The widget price is 10 dollars.</p></html>"
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    server, port, seen = await _serve(
        _server_context(certificates["matching"], certificates["matching_key"]),
        response,
    )
    try:
        parsed = urlsplit(f"https://pin.research.test:{port}/guide")
        result = await _pinned_exchange(
            parsed,
            "127.0.0.1",
            timeout=3,
            max_bytes=20_000,
            ssl_context=_client_context(certificates["ca"]),
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result.status == 200
    assert b"Pinned" in result.body
    assert seen[0][0][0] == "127.0.0.1"
    request = seen[0][1].decode()
    assert f"Host: pin.research.test:{port}" in request
    assert "pin.research.test" not in looked_up


@pytest.mark.asyncio
async def test_pinned_https_rejects_a_mismatched_or_untrusted_certificate(certificates):
    body = b"<html><title>Wrong</title><p>The widget price is 10 dollars.</p></html>"
    response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1\r\n\r\n" + body
    server, port, seen = await _serve(
        _server_context(certificates["other"], certificates["other_key"]),
        response,
    )
    try:
        parsed = urlsplit(f"https://pin.research.test:{port}/guide")
        with pytest.raises(FetchError) as mismatched:
            await _pinned_exchange(
                parsed,
                "127.0.0.1",
                timeout=3,
                max_bytes=20_000,
                ssl_context=_client_context(certificates["ca"]),
            )
        assert _certificate_failure(mismatched.value)
        assert seen == []
        with pytest.raises(FetchError) as untrusted:
            await _pinned_exchange(
                parsed,
                "127.0.0.1",
                timeout=3,
                max_bytes=20_000,
                ssl_context=None,
            )
        assert _certificate_failure(untrusted.value)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_transport_does_not_follow_a_redirect(certificates):
    response = b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/secret\r\nContent-Length: 0\r\n\r\n"
    server, port, seen = await _serve(
        _server_context(certificates["matching"], certificates["matching_key"]),
        response,
    )
    try:
        parsed = urlsplit(f"https://pin.research.test:{port}/start")
        result = await _pinned_exchange(
            parsed,
            "127.0.0.1",
            timeout=3,
            max_bytes=20_000,
            ssl_context=_client_context(certificates["ca"]),
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result.status == 302
    assert result.headers["location"] == "http://127.0.0.1/secret"
    assert len(seen) == 1
    assert b"/secret" not in seen[0][1]


@pytest.mark.asyncio
async def test_public_fetcher_still_rejects_the_local_certificate_host(certificates):
    calls = []

    async def resolver(host):
        return ["127.0.0.1"]

    async def connector(parsed, ip):
        calls.append(ip)
        raise AssertionError("loopback was contacted")

    fetcher = PageFetcher(resolver=resolver, connector=connector, limits=_limits())
    with pytest.raises(DestinationRejected):
        await fetcher.fetch("https://pin.research.test/guide")
    assert calls == []


def _certificate_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    return False
